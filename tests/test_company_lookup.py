"""
Coverage for company_lookup.resolve_company() -- the fuzzy name-to-ticker
grounding every LLM-extracted company name goes through (see classifier.py's
_ground_companies). Pure logic, no LLM/DB calls needed.

Real cases that exposed FIRST_WORD_CUTOFF (found via a manual audit of
already-grounded news_item_companies rows, see conversation history):
different real NSE companies that happen to share a generic industry-
descriptor word ("Financial Services", "Technologies", "Finance",
"Jewellers", "Laboratories") scored high enough on whole-string similarity
alone to falsely match each other, even though their actual distinguishing
word is completely different.

Run: ./.venv/bin/python -m pytest tests/test_company_lookup.py -v
"""
import company_lookup


def test_similar_but_different_companies_do_not_match():
    """Each pair here is two REAL, DIFFERENT NSE companies -- the second
    element must never be what the first one's name resolves to."""
    cases = [
        ("Avanse Financial Services", "ABANS"),
        ("Jio Financial Services", "JMFINANCIL"),
        ("Real Touch Finance", "LTF"),
        ("PNG Jewellers", "PCJEWELLER"),
        ("Colinz Laboratories", "VALIANTLAB"),
        ("Nalco", "NELCO"),
        ("Alphabet", "ALPHAGEO"),
    ]
    for guess, wrong_symbol in cases:
        matches = company_lookup.resolve_company(guess, cutoff=0.8)
        symbols = [m["symbol"] for m in matches]
        assert wrong_symbol not in symbols, f"{guess!r} incorrectly matched {wrong_symbol}"


def test_dixon_technologies_resolves_to_the_real_dixon():
    """Bonus effect of the fix, not just a rejection: before FIRST_WORD_CUTOFF,
    "Dixon Technologies" incorrectly matched KFIN TECHNOLOGIES LIMITED (shared
    "TECHNOLOGIES" inflated the whole-string score); with the fix, the
    correct real company (DIXON) wins instead of just getting no match."""
    matches = company_lookup.resolve_company("Dixon Technologies", cutoff=0.8)
    assert matches and matches[0]["symbol"] == "DIXON"


def test_genuine_matches_still_resolve_correctly():
    cases = {
        "Reliance Industries": "RELIANCE",
        "Sun Pharma": "SUNPHARMA",
        "Sun Pharmaceutical": "SUNPHARMA",
        "Larsen & Toubro": "LT",
        "Larsen and Toubro Ltd": "LT",
        "Tata Consultancy Services": "TCS",
        "State Bank of India": "SBIN",
        "HDFC Bank": "HDFCBANK",
    }
    for guess, expected_symbol in cases.items():
        matches = company_lookup.resolve_company(guess, cutoff=0.8)
        assert matches and matches[0]["symbol"] == expected_symbol, \
            f"{guess!r} should resolve to {expected_symbol}, got {matches}"


def test_bank_suffix_is_stripped_during_normalization():
    """"Bank" wasn't previously in the stripped-suffix list, alongside
    Ltd/Limited -- added so e.g. "HDFC Bank" normalizes to the same
    single distinguishing word ("HDFC") the listed company name does."""
    assert company_lookup.normalize("HDFC Bank") == "HDFC"
    assert company_lookup.normalize("ICICI Bank Limited") == "ICICI"
