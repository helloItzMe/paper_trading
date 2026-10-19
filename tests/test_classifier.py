"""
Coverage for the LLM classification step (classifier.py) -- the one part of
the pipeline that isn't deterministic code, so it's the part most likely to
silently regress without a test catching it. Runs against the FIXED
synthetic cases in mock_servers/fixtures/news_fixture.json (see
mock_servers/generate_news_fixture.py) -- real headlines, hand-written to
hit each classifier branch, referencing real company names so grounding is
exercised for real.

This makes one real `claude` CLI call (all synthetic items batched together)
and asserts against it, so these tests exercise the actual model, not a
stub. That means: skip cleanly if the CLI isn't available (CI without
Claude Code, no network, etc), and accept that a genuine LLM misjudgment on
a borderline case is a possible (rare) flake, not a hard guarantee -- these
tests catch code-path regressions (wrong field, broken threshold, grounding
bug), not perfect LLM behavior under all phrasings.

Run: ./.venv/bin/python -m pytest tests/test_classifier.py -v
"""
import json
import os

import pytest

import classifier
import db
import trade_bot

FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mock_servers", "fixtures", "news_fixture.json",
)


@pytest.fixture(scope="module")
def classified_by_case():
    try:
        classifier.ensure_available()
    except RuntimeError as e:
        pytest.skip(f"claude CLI not available: {e}")

    with open(FIXTURE_PATH) as f:
        items = json.load(f)
    synthetic = [it for it in items if it.get("synthetic") and it.get("case") != "stale_should_be_discarded"]
    assert synthetic, "fixture has no synthetic cases -- did generate_news_fixture.py run?"

    enrich_items = [
        {"title": it["title"], "link": "http://test.invalid", "source": it["source"], "pub_date": None}
        for it in synthetic
    ]
    classifier.enrich(enrich_items, model=classifier.DEFAULT_MODEL)
    return {it["case"]: enriched for it, enriched in zip(synthetic, enrich_items)}


# ---- raw classification output ----

def test_order_win_material(classified_by_case):
    r = classified_by_case["order_win_material"]
    assert r["event_type"] == "ORDER_WIN"
    assert r["order_is_material"] is True
    assert r["mentioned_companies"], "expected at least one grounded company"


def test_order_win_non_material(classified_by_case):
    r = classified_by_case["order_win_non_material"]
    assert r["event_type"] == "ORDER_WIN"
    assert r["order_is_material"] is False


def test_profit_increase_above_threshold(classified_by_case):
    r = classified_by_case["profit_increase_above_threshold"]
    assert r["event_type"] == "PROFIT_INCREASE"
    assert r["profit_pct"] is not None and r["profit_pct"] >= 35


def test_profit_increase_below_threshold(classified_by_case):
    r = classified_by_case["profit_increase_below_threshold"]
    assert r["event_type"] == "PROFIT_INCREASE"
    assert r["profit_pct"] is not None and r["profit_pct"] < 35


def test_revenue_growth_above_threshold(classified_by_case):
    r = classified_by_case["revenue_growth_above_threshold"]
    assert r["event_type"] == "REVENUE_GROWTH"
    assert r["revenue_pct"] is not None and r["revenue_pct"] >= 20


def test_multi_company_roundup_extracts_all_companies(classified_by_case):
    r = classified_by_case["multi_company_roundup"]
    assert len(r["mentioned_companies"]) >= 2, "roundup headline should ground more than one company"


def test_order_loss_is_negative_signal(classified_by_case):
    r = classified_by_case["order_loss_negative"]
    assert r["event_type"] == "ORDER_LOSS"
    assert r["category"] == "order"
    assert r["signal"] == "negative"


def test_profit_decrease_is_negative_signal(classified_by_case):
    r = classified_by_case["profit_decrease_negative"]
    assert r["event_type"] == "PROFIT_DECREASE"
    assert r["signal"] == "negative"


def test_other_irrelevant_extracts_nothing(classified_by_case):
    r = classified_by_case["other_irrelevant"]
    assert r["event_type"] == "OTHER"
    assert r["mentioned_companies"] == []


def test_no_invented_tickers(classified_by_case):
    """The LLM extracts company NAMES only -- every grounded symbol must be a
    real, verified NSE company, never something the model made up."""
    real_symbols = set(db.get_all_companies().keys())
    checked = 0
    for r in classified_by_case.values():
        for c in r["mentioned_companies"]:
            assert c["symbol"] in real_symbols, f"invented/unverified symbol: {c['symbol']}"
            checked += 1
    assert checked > 0, "no companies were grounded across any case -- test setup is broken"


# ---- grounding cutoff (no LLM call needed -- pure company_lookup scoring) ----
# Real case that exposed this: a "Microsoft, Amazon and Alphabet earnings..."
# headline (event_type OTHER, so never a buy candidate) still got "Alphabet"
# grounded to ALPHAGEO (INDIA) LIMITED -- a real but wholly unrelated NSE
# company -- purely from difflib.SequenceMatcher string-similarity
# coincidence (see classifier.GROUNDING_CUTOFF's notes).

def test_foreign_tech_names_do_not_ground_to_unrelated_nse_companies():
    for name in ("Alphabet", "Microsoft", "Amazon", "Apple", "Meta", "Nvidia", "Tesla"):
        headline = f"{name} earnings reveal the next challenge in the AI race"
        assert classifier._ground_companies([name], headline) == [], \
            f"{name!r} incorrectly grounded to a real NSE company"


def test_genuine_company_names_still_ground_correctly():
    cases = {
        "Reliance Industries": ("Reliance Industries reports strong Q1 profit growth", "RELIANCE"),
        "Sun Pharma": ("Sun Pharma Q1 profit jumps 27%", "SUNPHARMA"),
        "Sun Pharmaceutical": ("Sun Pharmaceutical posts strong quarterly results", "SUNPHARMA"),
        "Larsen & Toubro": ("Larsen & Toubro wins large infrastructure order", "LT"),
        "Larsen and Toubro Ltd": ("Larsen and Toubro Ltd bags new contract", "LT"),
        "Tata Consultancy Services": ("Tata Consultancy Services beats estimates", "TCS"),
        "State Bank of India": ("State Bank of India reports higher NIM", "SBIN"),
        "HDFC Bank": ("HDFC Bank shares rise on strong earnings", "HDFCBANK"),
    }
    for name, (headline, expected_symbol) in cases.items():
        grounded = classifier._ground_companies([name], headline)
        assert grounded and grounded[0]["symbol"] == expected_symbol, \
            f"{name!r} should ground to {expected_symbol}, got {grounded}"


def test_single_word_target_rejected_when_word_absent_from_headline():
    """Even a high-scoring match should be rejected if the matched company's
    own (single-word, post-normalize) name never actually appears in the
    headline -- belt-and-suspenders on top of GROUNDING_CUTOFF, since a
    cutoff alone only guards against the specific collisions already found,
    not every possible one."""
    headline = "Some company announces a new product roadmap for next year"
    assert classifier._ground_companies(["Reliance Industries"], headline) == []


# ---- downstream decision logic (trade_bot._qualifies / _companies_for_item) ----
# The classifier's output only matters insofar as it drives the buy/queue
# decision correctly -- these tie the LLM step to that deterministic logic.

def test_material_order_win_qualifies_auto(classified_by_case):
    r = classified_by_case["order_win_material"]
    # _qualifies(r) alone (no symbol arg) falls back to the 'sme' tier, so
    # this still passes -- order_is_material is True and the headline
    # doesn't claim a price move. But this fixture's company is RELIANCE, a
    # genuine real top-100 (Nifty 50) large-cap -- once _companies_for_item
    # resolves the real symbol and applies the large-cap exclusion (see
    # _qualifies' ORDER_WIN branch), it correctly yields NO candidates:
    # large caps win orders constantly, that alone isn't a buy signal.
    assert trade_bot._qualifies(r) is True
    assert trade_bot._company_tier("RELIANCE") == "large"
    assert trade_bot._companies_for_item(r) == {}


def test_material_order_win_qualifies_auto_for_non_large_cap():
    # Same signal shape as above, but for a real company OUTSIDE the top 100
    # (see test_sme_tier_needs_the_higher_bar) -- order-win auto-qualification
    # still works when the large-cap exclusion doesn't apply.
    assert trade_bot._company_tier("PRIMECAB") == "sme"
    it = {"event_type": "ORDER_WIN", "order_is_material": True,
          "title": "Primecab wins Rs 50 crore order from state transport corporation"}
    assert trade_bot._qualifies(it, "PRIMECAB") is True


def test_large_cap_order_win_never_qualifies_even_if_material():
    assert trade_bot._company_tier("RELIANCE") == "large"
    it = {"event_type": "ORDER_WIN", "order_is_material": True,
          "title": "Reliance Industries wins Rs 50,000 crore order from Indian Railways"}
    assert trade_bot._qualifies(it, "RELIANCE") is False


def test_non_material_order_win_does_not_qualify(classified_by_case):
    r = classified_by_case["order_win_non_material"]
    assert trade_bot._qualifies(r) is False
    assert trade_bot._companies_for_item(r) == {}


# ---- regex percentage extraction: real headline shapes verified against
# news_items in production (see extract_profit_pct_regex/extract_revenue_pct_regex) ----

@pytest.mark.parametrize("title,expected", [
    ("Sportking India net profit jumps 123% in Q1FY27 as EBITDA surges 90%", 123.0),
    ("India ITC profit falls 12% amid cigarette tax hike", -12.0),
    ("Firm posts 15% profit decline in Q1", -15.0),
    ("Sun Pharma sees 27% profit surge in Q1FY27, driven by India growth", 27.0),
])
def test_extract_profit_pct_regex(title, expected):
    assert classifier.extract_profit_pct_regex(title) == expected


@pytest.mark.parametrize("title,expected", [
    ("Akasa Air Reports 37% Revenue Growth in FY2025-26", 37.0),
    ("Rossell Techsys posts 78% revenue growth in Q1 FY27", 78.0),
    ("Apple hails India as a fastest-growing market after 16 per cent Q2 revenue jump", 16.0),
    ("Embassy REIT Reports 17% YoY Growth in Revenue and Net Operating Income", 17.0),
    ("Company revenue declines 8% YoY on weak demand", -8.0),
])
def test_extract_revenue_pct_regex(title, expected):
    assert classifier.extract_revenue_pct_regex(title) == expected


def test_extract_pct_regex_ignores_unrelated_percentages():
    # "order intake surges 50%" -- a real percentage, but not a profit/revenue
    # one; must not be misattributed to either.
    title = "ABB India Q2 profit misses estimates; order intake surges 50%"
    assert classifier.extract_profit_pct_regex(title) is None
    assert classifier.extract_revenue_pct_regex(title) is None


def test_profit_below_threshold_does_not_qualify(classified_by_case):
    r = classified_by_case["profit_increase_below_threshold"]
    assert trade_bot._qualifies(r) is False


# ---- order-win headlines that announce their own price move (real case:
# "Inox India Share Price Gains 5% On Rs 939-Crore Worth Multiple Order
# Wins") -- no LLM call needed, pure regex + a hand-built classified dict ----

def test_order_win_headline_announcing_price_move_does_not_qualify():
    it = {"event_type": "ORDER_WIN", "order_is_material": True,
          "title": "Inox India Share Price Gains 5% On Rs 939-Crore Worth Multiple Order Wins - NDTV Profit"}
    assert trade_bot._qualifies(it) is False


def test_order_win_headline_without_price_move_still_qualifies():
    it = {"event_type": "ORDER_WIN", "order_is_material": True,
          "title": "RELIANCE INDUSTRIES LTD bags mega Rs 15,000 crore order from Indian Railways"}
    assert trade_bot._qualifies(it) is True


def test_profit_above_threshold_with_confident_pct_qualifies_auto(classified_by_case):
    # "net profit surges 42%" is a bare, plainly-stated percentage --
    # classifier.enrich() regex-extracts it deterministically (see
    # extract_profit_pct_regex), so pct_low_confidence is False and this
    # auto-qualifies instead of needing manual review.
    r = classified_by_case["profit_increase_above_threshold"]
    assert trade_bot._qualifies(r) is True
    assert r["pct_low_confidence"] is False
    candidates = trade_bot._companies_for_item(r)
    assert candidates
    assert all(confidence == "auto" for _, confidence, _ in candidates.values())


def test_profit_with_no_bare_pct_and_low_llm_confidence_qualifies_manual():
    # No regex-parseable percentage in the headline at all (profit stated as
    # an absolute figure, not a percentage) -- if the LLM's own confidence in
    # a computed/inferred number is below 50, this must land on manual, not
    # auto-buy off an unreliable extraction. Hand-built dict, no LLM call.
    it = {
        "event_type": "PROFIT_INCREASE", "category": "earnings",
        "title": "Some Company profit rises to Rs 120 crore from Rs 54 crore last year",
        "profit_pct": 122.2, "revenue_pct": None, "pct_low_confidence": True,
        "mentioned_companies": [{"symbol": "SOMECO", "name": "Some Company", "match_type": "llm_extracted"}],
    }
    assert trade_bot._qualifies(it) is True
    candidates = trade_bot._companies_for_item(it)
    assert candidates
    assert all(confidence == "manual" for _, confidence, _ in candidates.values())


def test_revenue_only_signal_always_qualifies_manual_even_if_regex_confident():
    # Regex-extracted, unambiguous revenue percentage with no profit_pct at
    # all stated -- still forced to manual, per _item_pct_confident's
    # revenue-only rule (a revenue-only headline isn't trusted enough to
    # auto-buy on by itself, regardless of extraction confidence).
    it = {
        "event_type": "REVENUE_GROWTH", "category": "earnings",
        "title": "Some Company revenue grows 40% YoY on strong export demand",
        "profit_pct": None, "revenue_pct": 40.0, "pct_low_confidence": True,
        "mentioned_companies": [{"symbol": "SOMECO", "name": "Some Company", "match_type": "llm_extracted"}],
    }
    assert trade_bot._qualifies(it) is True
    candidates = trade_bot._companies_for_item(it)
    assert candidates
    assert all(confidence == "manual" for _, confidence, _ in candidates.values())


def test_profit_pct_disagreement_between_regex_and_llm_is_low_confidence():
    profit_pct, confident = classifier._combine_pct(regex_val=42.0, llm_val=15.0, llm_confidence=90)
    assert profit_pct == 42.0  # regex still wins the value...
    assert confident is False  # ...but the mismatch itself is flagged unconfident


def test_profit_pct_regex_and_llm_agreement_is_confident():
    profit_pct, confident = classifier._combine_pct(regex_val=42.0, llm_val=42.0, llm_confidence=10)
    assert profit_pct == 42.0
    assert confident is True  # regex-backed, doesn't need the LLM's own confidence to be high


def test_profit_pct_no_regex_signal_falls_back_to_llm_confidence():
    below, confident_below = classifier._combine_pct(regex_val=None, llm_val=30.0, llm_confidence=40)
    above, confident_above = classifier._combine_pct(regex_val=None, llm_val=30.0, llm_confidence=60)
    assert (below, confident_below) == (30.0, False)
    assert (above, confident_above) == (30.0, True)


# ---- _item_pct_confident: profit/revenue presence asymmetry ----

def test_item_confidence_profit_only_rests_on_profit_alone():
    assert classifier._item_pct_confident(42.0, True, None, True) is True
    assert classifier._item_pct_confident(42.0, False, None, True) is False


def test_item_confidence_revenue_only_is_always_low_confidence():
    # Even a regex-backed, fully-confident revenue number doesn't count --
    # revenue_pct alone is never trusted enough to auto-buy on.
    assert classifier._item_pct_confident(None, True, 30.0, True) is False
    assert classifier._item_pct_confident(None, True, 30.0, False) is False


def test_item_confidence_both_present_requires_both_confident():
    assert classifier._item_pct_confident(42.0, True, 15.0, True) is True
    assert classifier._item_pct_confident(42.0, True, 15.0, False) is False
    assert classifier._item_pct_confident(42.0, False, 15.0, True) is False


def test_item_confidence_neither_present_is_vacuously_confident():
    assert classifier._item_pct_confident(None, True, None, True) is True


@pytest.mark.parametrize("case", ["order_loss_negative", "profit_decrease_negative"])
def test_negative_events_never_qualify(classified_by_case, case):
    assert trade_bot._qualifies(classified_by_case[case]) is False


def test_other_never_qualifies(classified_by_case):
    assert trade_bot._qualifies(classified_by_case["other_irrelevant"]) is False


# ---- tiered earnings thresholds (no LLM call needed -- pure logic on a
# hand-built classified dict; large-vs-sme is a real DB lookup though,
# backed by NSE's actual Nifty 50 + Nifty Next 50 lists -- see broker/large_cap.py) ----

def test_sme_tier_needs_the_higher_bar():
    # PRIMECAB is real and NSE-listed, but nowhere near NSE's real top 100
    # by market cap (not in Nifty 50 or Nifty Next 50) -- "sme" tier.
    assert trade_bot._company_tier("PRIMECAB") == "sme"
    it = {"event_type": "PROFIT_INCREASE", "profit_pct": 25, "revenue_pct": None}
    assert trade_bot._qualifies(it, "PRIMECAB") is False  # 25% < SME's 35% bar
    it["profit_pct"] = 36
    assert trade_bot._qualifies(it, "PRIMECAB") is True


def test_large_cap_qualifies_on_a_smaller_move():
    # RELIANCE is genuinely in NSE's real Nifty 50 -- true top-100 large cap.
    assert trade_bot._company_tier("RELIANCE") == "large"
    it = {"event_type": "PROFIT_INCREASE", "profit_pct": 25, "revenue_pct": None}
    # Same 25% that fails PRIMECAB above should pass for a real large-cap.
    assert trade_bot._qualifies(it, "RELIANCE") is True


def test_no_symbol_falls_back_to_the_stricter_sme_bar():
    # 25% clears large's 20% bar but NOT sme's 35% bar -- an unresolved
    # symbol must get the harder bar by default, never the easier one.
    it = {"event_type": "PROFIT_INCREASE", "profit_pct": 25, "revenue_pct": None}
    assert trade_bot._qualifies(it) is False


# ---- freshness gate (no LLM call needed -- pure date math) ----

def test_stale_item_is_discarded_before_classification():
    from datetime import datetime, timedelta, timezone

    with open(FIXTURE_PATH) as f:
        items = json.load(f)
    stale = next(it for it in items if it.get("case") == "stale_should_be_discarded")
    # Pinned to a fixed real weekday, not real wall-clock "now" -- this
    # checks the plain age-threshold logic, independent of the separate
    # weekend-carryover behavior (see the two tests below) and independent
    # of which real calendar day the suite happens to run on (this test used
    # to use datetime.now() directly, which broke when actually run on a
    # weekend -- see those tests for why that's not itself a bug).
    fixed_now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)  # a Tuesday
    db.set_clock(lambda symbol: fixed_now)
    try:
        pub_date = fixed_now - timedelta(minutes=stale["age_minutes"])
        assert not trade_bot._is_fresh_enough({"pub_date": pub_date})
    finally:
        db.set_clock(None)


def test_weekend_news_is_stale_before_next_market_open():
    """Deliberate behavior change (was test_weekend_news_not_discarded_
    before_next_market_open, which asserted the OPPOSITE): a weekend
    headline checked before the next trading day's market open is now
    STALE, not fresh. Verified live and real: the old `age <= NEWS_MAX_AGE`
    check let a NEGATIVE age (checking before the rolled-forward reference
    point) trivially pass regardless of true elapsed time -- a Saturday-
    dated article, checked Monday 05:54 (before the 09:15 open), sailed
    through and auto-queued a buy candidate on what turned out to be a
    republished article actually ~2 years old. There is no way to tell a
    legitimate weekend scoop checked early apart from old content wearing a
    fake weekend date using timing alone, so the safer rule wins: not fresh
    until the market has actually had a chance to react."""
    from datetime import datetime

    pub_date = datetime(2026, 8, 1, 10, 0, tzinfo=trade_bot.IST)   # Saturday morning
    fixed_now = datetime(2026, 8, 2, 20, 0, tzinfo=trade_bot.IST)  # Sunday evening -- before Monday's open
    db.set_clock(lambda symbol: fixed_now)
    try:
        assert not trade_bot._is_fresh_enough({"pub_date": pub_date})
    finally:
        db.set_clock(None)


def test_weekend_news_fresh_right_after_market_open():
    """The legitimate half of the weekend-carryover behavior still holds --
    a weekend headline is fresh starting exactly at the next trading day's
    market open (not before), for the normal NEWS_MAX_AGE window from
    there."""
    from datetime import datetime

    pub_date = datetime(2026, 8, 1, 10, 0, tzinfo=trade_bot.IST)   # Saturday morning
    fixed_now = datetime(2026, 8, 3, 9, 30, tzinfo=trade_bot.IST)  # Monday 09:30 -- 15min after open
    db.set_clock(lambda symbol: fixed_now)
    try:
        assert trade_bot._is_fresh_enough({"pub_date": pub_date})
    finally:
        db.set_clock(None)


def test_weekend_news_discarded_well_after_next_market_open():
    """Once the next trading day's market has been open for well over
    NEWS_MAX_AGE, a weekend headline is stale again, same as any other."""
    from datetime import datetime

    pub_date = datetime(2026, 8, 1, 10, 0, tzinfo=trade_bot.IST)   # Saturday morning
    fixed_now = datetime(2026, 8, 3, 12, 0, tzinfo=trade_bot.IST)  # Monday noon
    db.set_clock(lambda symbol: fixed_now)
    try:
        assert not trade_bot._is_fresh_enough({"pub_date": pub_date})
    finally:
        db.set_clock(None)
