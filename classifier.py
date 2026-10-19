#!/usr/bin/env python3
"""
Source-agnostic news enrichment: money extraction (regex), company mentions
(company_lookup's exact scanner), and event classification (Claude, via the
local `claude` CLI, replacing the old brittle keyword-substring matching).

Deliberately separate from news_bot.py (which only fetches Google News/RSS) so
the same enrichment pipeline works on items from ANY source -- Twitter, other
RSS feeds, whatever gets added later -- as long as they're shaped like:
    {"title": str, "link": str, "source": str, "pub_date": datetime|None}

Classification runs through the local `claude` CLI (Claude Code) in headless
mode (`claude -p ...`), not the Anthropic API directly -- this piggybacks on
the same login/subscription already used to develop this project, no
separate API key or billing setup needed.

Usage as a library:
    import classifier
    items = classifier.enrich(items)   # adds money, mentioned_companies, event_type, category, signal
"""
import json
import os
import re
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "broker"))
import company_lookup  # noqa: E402 -- needs sys.path set up first

# CLAUDE_CODE_EXECPATH is set when running inside a Claude Code session
# (points at the exact binary in use); fall back to whatever `claude` resolves
# to on PATH for standalone runs (cron, plain terminal, etc).
CLAUDE_BIN = os.environ.get("CLAUDE_CODE_EXECPATH") or shutil.which("claude") or "claude"
DEFAULT_MODEL = "haiku"
BATCH_SIZE = 30

MONEY_RE = re.compile(
    r"(?:US\$|\$|₹|Rs\.?|INR|EUR|€|£)\s?[\d,]+(?:\.\d+)?\s?"
    r"(?:crore|lakh|million|billion|bn|mn|M|B|K|Cr\.?)?",
    re.IGNORECASE,
)

# Deterministic cross-check for profit_pct/revenue_pct, independent of the LLM.
# Exists because the LLM's own extraction was verified inconsistent across
# identical runs (a clearly-stated "5%" missed 1-in-3 tries -- see CLAUDE.md's
# "Design decisions" section) -- that's a *silent omission* failure mode, which
# an LLM-reported confidence score can't fix (a model that missed the number
# won't know to flag itself as unsure). Regex is the actual fix for that;
# LLM confidence only covers the remaining case regex can't parse (e.g.
# "profit rises to Rs 120 crore from Rs 54 crore" -- no bare percentage to
# find at all). \b around PAT avoids matching inside "PATANJALI".
PROFIT_KEYWORDS = r"(?:net\s+profit|profit\s+after\s+tax|\bpat\b|profit|net\s+income)"
REVENUE_KEYWORDS = r"(?:net\s+sales|revenue|turnover|\bsales\b)"
# Includes noun forms ("growth", "surge") alongside verbs -- verified live
# against real classified headlines in news_items: "Akasa Air Reports 37%
# Revenue Growth", "Sun Pharma sees 27% profit surge" use the keyword-then-
# noun order below, not a verb at all. Without "growth" here specifically,
# 13/137 real headlines with a plainly-stated percentage went unmatched.
RISE_WORDS = r"(?:rises?|rose|jumps?|surges?|soars?|grows?|grew|growth|gains?|climbs?|zooms?|up|increases?|expands?|beats?)"
DECLINE_WORDS = r"(?:falls?|fell|drops?|declines?|slips?|dips?|down|narrows?|widens?|shrinks?|plunges?|tumbles?|contracts?)"
PCT_UNIT = r"(?:%|per\s?cent|pc\b)"


def _regex_pct(title, keyword_pat):
    """Deterministic signed percentage extraction for a keyword group (profit
    or revenue). Tries three headline shapes, in order:
      1. "<keyword> ... <direction word> ... N%" (e.g. "net profit jumps 123%")
      2. "N% ... <direction word> ... <keyword>" (e.g. "123% jump in net profit")
      3. "N% ... <keyword> <direction word>" (e.g. "37% Revenue Growth",
         "16 per cent ... revenue jump", "27% profit surge") -- the number
         comes first and the direction word trails immediately after the
         keyword instead of leading into it; (1) and (2) alone both miss this
         shape, which is common in real headlines using the noun form of the
         direction word ("growth", "surge") rather than a verb.
    Returns None if no bare percentage is found near the keyword at all --
    callers should fall back to the LLM's value in that case."""
    forward_rise = re.compile(
        rf"\b{keyword_pat}\b.{{0,25}}?\b(?:{RISE_WORDS})\b.{{0,12}}?(\d+(?:\.\d+)?)\s?{PCT_UNIT}",
        re.IGNORECASE,
    )
    forward_decline = re.compile(
        rf"\b{keyword_pat}\b.{{0,25}}?\b(?:{DECLINE_WORDS})\b.{{0,12}}?(\d+(?:\.\d+)?)\s?{PCT_UNIT}",
        re.IGNORECASE,
    )
    reverse_rise = re.compile(
        rf"(\d+(?:\.\d+)?)\s?{PCT_UNIT}.{{0,12}}?\b(?:{RISE_WORDS})\b.{{0,25}}?\b{keyword_pat}\b",
        re.IGNORECASE,
    )
    reverse_decline = re.compile(
        rf"(\d+(?:\.\d+)?)\s?{PCT_UNIT}.{{0,12}}?\b(?:{DECLINE_WORDS})\b.{{0,25}}?\b{keyword_pat}\b",
        re.IGNORECASE,
    )
    number_keyword_rise = re.compile(
        rf"(\d+(?:\.\d+)?)\s?{PCT_UNIT}.{{0,20}}?\b{keyword_pat}\b\s+(?:{RISE_WORDS})\b",
        re.IGNORECASE,
    )
    number_keyword_decline = re.compile(
        rf"(\d+(?:\.\d+)?)\s?{PCT_UNIT}.{{0,20}}?\b{keyword_pat}\b\s+(?:{DECLINE_WORDS})\b",
        re.IGNORECASE,
    )
    for pat, sign in (
        (forward_rise, 1), (reverse_rise, 1), (number_keyword_rise, 1),
        (forward_decline, -1), (reverse_decline, -1), (number_keyword_decline, -1),
    ):
        m = pat.search(title)
        if m:
            return sign * float(m.group(1))
    return None


def extract_profit_pct_regex(title):
    return _regex_pct(title, PROFIT_KEYWORDS)


def extract_revenue_pct_regex(title):
    return _regex_pct(title, REVENUE_KEYWORDS)


# Two non-null percentages agreeing this loosely (absolute points) counts as
# "the same number" -- rounding/whole-vs-decimal differences shouldn't force
# manual review when regex and the LLM already agree in substance.
PCT_AGREEMENT_TOLERANCE = 2.0

EVENT_TYPES = [
    "ORDER_WIN", "ORDER_LOSS",
    "PROFIT_INCREASE", "PROFIT_DECREASE",
    "REVENUE_GROWTH", "REVENUE_DECLINE",
    "OTHER",
]

# event_type -> (category, signal) for downstream consumers (trade_bot.py etc.)
EVENT_META = {
    "ORDER_WIN": ("order", "positive"),
    "ORDER_LOSS": ("order", "negative"),
    "PROFIT_INCREASE": ("earnings", "positive"),
    "PROFIT_DECREASE": ("earnings", "negative"),
    "REVENUE_GROWTH": ("earnings", "positive"),
    "REVENUE_DECLINE": ("earnings", "negative"),
    "OTHER": ("other", "neutral"),
}

PROMPT_TEMPLATE = """You are analyzing Indian stock-market news headlines. For each numbered headline:

1. Pick the ONE event type that best describes it:
   - ORDER_WIN: company won/received/bagged a new order, contract, or deal
   - ORDER_LOSS: company lost, had cancelled, or was denied an order/contract
   - PROFIT_INCREASE: profit / net income / PAT rose, beat expectations, or surged
   - PROFIT_DECREASE: profit / net income / PAT fell, missed expectations, or a loss widened/narrowed
   - REVENUE_GROWTH: revenue / sales / turnover grew
   - REVENUE_DECLINE: revenue / sales / turnover fell
   - OTHER: anything else (fundraises, general news, unrelated)

2. List every actual COMPANY mentioned, exactly as written in the headline
   (e.g. "Union Bank of India", "MOIL", "MTAR Tech"). A headline can mention
   more than one company (roundups like "Buzzing Stocks: X gains...; Y jumps...").
   Do NOT include: the publisher/source name, stock indices (Nifty, Sensex),
   or generic words that aren't actually a company name.

3. If the headline states a profit growth/decline percentage, extract it as
   profit_pct (a signed number, e.g. 52 for "profit jumps 52%", -18 for
   "profit falls 18%"). null if no profit percentage is stated.

4. If the headline states a revenue/sales growth/decline percentage, extract
   it as revenue_pct, same signed-number convention. null if not stated.

5. If the event is ORDER_WIN, judge whether the order size sounds large/
   material for that company specifically (not just a large absolute number --
   a ₹50 crore order is material for a small company, trivial for a giant one)
   as order_is_material: true/false. false for any other event type.

6. Rate your own confidence in the profit_pct/revenue_pct numbers you just
   extracted, 0-100, as pct_confidence. 100 if the headline states the
   percentage plainly and unambiguously; lower if you had to infer/compute it,
   the headline is ambiguous about which metric it refers to, or both
   profit_pct and revenue_pct are null (nothing to be confident about -- use 0).

Headlines:
{headlines}

Respond with ONLY a JSON array, one object per headline. No other text before
or after it -- no commentary, no explanation of your approach, nothing:
[{{"i": 1, "event": "ORDER_WIN", "companies": ["Company Name"], "profit_pct": null, "revenue_pct": null, "order_is_material": true, "pct_confidence": 0}}, ...]"""


def extract_money(title):
    matches = MONEY_RE.findall(title)
    return matches[0] if matches else None


# "companies" asks the LLM for entity extraction (which names are mentioned),
# NOT ticker resolution -- the LLM never gets to invent a symbol. Each
# extracted name is separately grounded through company_lookup.resolve_company()
# against the real, verified NSE company list in enrich() below. This fixes
# the substring-containment/word-collision bugs the old pure-text scanner had
# (e.g. "Bank of India" matching inside "Union Bank of India", or "TOTAL"/
# "WEALTH"/"RETAIL" matching as ordinary English words) since the LLM
# understands entity boundaries and context, and can also see through
# publisher-name suffixes ("... - NDTV Profit") without being told to.
#
# Expected shape per headline (enforced by parsing/validation below, not by
# the model -- the `claude` CLI's -p mode has no equivalent to Ollama's
# schema-constrained "format" field):
#   {"i": int, "event": one of EVENT_TYPES, "companies": [str, ...],
#    "profit_pct": number|null, "revenue_pct": number|null, "order_is_material": bool}


class ClassifierTimeout(Exception):
    """A single batch took too long -- soft failure, caller should skip this
    batch (defaults apply) and continue, not abort the whole run."""


CLAUDE_TIMEOUT = 150  # CLI startup + model latency; observed live variance up to ~90s for a 27-item batch
PREFLIGHT_TIMEOUT = 10  # `--version` is a local, no-network check -- should be near-instant


def ensure_available():
    """Fail fast, before doing any RSS/NSE fetch work, if the `claude` CLI
    isn't usable -- a real classification call costs several seconds (CLI
    startup + a live model round-trip) per batch, so finding out it's broken
    on the first of possibly dozens of batches wastes a fetch pass first.
    `--version` makes no API call, so this is fast and free."""
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--version"], capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"Can't find the `claude` CLI at '{CLAUDE_BIN}'. "
            f"Is Claude Code installed and on PATH (or CLAUDE_CODE_EXECPATH set)?"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"`claude --version` didn't respond within {PREFLIGHT_TIMEOUT}s -- something's wrong.")
    if result.returncode != 0:
        raise RuntimeError(f"`claude --version` exited {result.returncode}: {result.stderr.strip()[:500]}")


def _call_claude(prompt, model):
    cmd = [
        CLAUDE_BIN, "-p", prompt, "--model", model, "--output-format", "text",
        "--no-session-persistence", "--allowedTools", "", "--setting-sources", "",
        "--strict-mcp-config",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ClassifierTimeout(f"claude CLI didn't respond within {CLAUDE_TIMEOUT}s for this batch.")
    except FileNotFoundError:
        raise RuntimeError(
            f"Can't find the `claude` CLI at '{CLAUDE_BIN}'. "
            f"Is Claude Code installed and on PATH (or CLAUDE_CODE_EXECPATH set)?"
        )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr.strip()[:500]}")
    return result.stdout


# Claude sometimes wraps JSON in a markdown code fence despite being told not
# to -- strip ```json / ``` fences before parsing rather than failing the batch.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# Observed live, running through the `claude` CLI's default coding-agent
# system prompt: it occasionally decides this text-classification task is
# worth a meta-commentary preamble before the actual answer -- e.g. "This
# task doesn't require code changes or planning -- it's a direct text
# classification task. Here's the analysis: [...]". That prefix isn't a code
# fence, so _strip_fences alone doesn't catch it and json.loads fails on the
# whole string even though a perfectly valid array is sitting right there.
# Pulling out the first '[' through the last ']' is robust to any prose
# wrapped around the array, whichever side it's on.
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _strip_fences(text):
    return _FENCE_RE.sub("", text.strip())


def _extract_json_array(text):
    """Raises json.JSONDecodeError (uncaught) if no valid array can be found
    -- callers already handle that the same way as any other parse failure."""
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_ARRAY_RE.search(text)
    if not m:
        raise json.JSONDecodeError("No JSON array found in output", text, 0)
    return json.loads(m.group(0))


def _default_result():
    return {"event": "OTHER", "companies": [], "profit_pct": None, "revenue_pct": None,
            "order_is_material": False, "pct_confidence": 0}


def _coerce_pct(value):
    return value if isinstance(value, (int, float)) else None


# Below this, the LLM's own profit_pct/revenue_pct isn't trusted on its own --
# only used when regex found nothing to cross-check against at all.
LLM_PCT_CONFIDENCE_THRESHOLD = 50


def _combine_pct(regex_val, llm_val, llm_confidence):
    """Regex is authoritative when it finds a number -- it's deterministic, so
    it fixes the actual documented failure mode (LLM silently missing a
    plainly-stated percentage), which a self-reported LLM confidence score
    can't catch on its own. If the LLM also produced a number and it disagrees
    with regex beyond PCT_AGREEMENT_TOLERANCE, that's evidence the headline
    parse is suspect -- use the regex value but mark it unconfident rather
    than silently trusting either one. With no regex signal at all (e.g. "profit
    rises to Rs 120 crore from Rs 54 crore" -- nothing to compute from), fall
    back to the LLM's value gated by its own stated confidence."""
    if regex_val is not None:
        if llm_val is not None and abs(llm_val - regex_val) > PCT_AGREEMENT_TOLERANCE:
            return regex_val, False
        return regex_val, True
    if llm_val is None:
        return None, True
    return llm_val, llm_confidence >= LLM_PCT_CONFIDENCE_THRESHOLD


def _item_pct_confident(profit_pct, profit_confident, revenue_pct, revenue_confident):
    """Item-level confidence, asymmetric between profit and revenue:
        - both stated: only confident if BOTH are individually confident --
          a headline with two numbers is only as trustworthy as its weaker one.
        - profit only: confidence rests on profit alone (revenue was never
          mentioned, so it can't drag anything down).
        - revenue only, no profit at all: never confident, regardless of how
          sure regex/the LLM are about the revenue number -- a revenue-only
          headline isn't treated as reliable enough to auto-buy on by itself.
        - neither stated: nothing to be unconfident about (_qualifies()
          would reject the item anyway since both are None)."""
    if profit_pct is not None and revenue_pct is not None:
        return profit_confident and revenue_confident
    if profit_pct is not None:
        return profit_confident
    if revenue_pct is not None:
        return False
    return True


def classify_headlines_llm_batches(titles, model=DEFAULT_MODEL, batch_size=BATCH_SIZE):
    """Generator: yields (start_index, batch_results) as each batch completes --
    for callers that want to persist/log progress incrementally instead of
    waiting for the entire title list to finish (a large backlog can take many
    minutes; without incremental persistence, an interruption loses everything)."""
    for start in range(0, len(titles), batch_size):
        batch = titles[start:start + batch_size]
        batch_results = [_default_result() for _ in batch]
        headlines_block = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(batch))
        prompt = PROMPT_TEMPLATE.format(headlines=headlines_block)

        try:
            raw_text = _call_claude(prompt, model)
        except ClassifierTimeout as e:
            print(f"[warn] {e} Leaving this batch of {len(batch)} headlines as OTHER, continuing.",
                  file=sys.stderr)
            yield start, batch_results
            continue
        try:
            parsed = _extract_json_array(raw_text)
        except json.JSONDecodeError:
            print(f"[warn] claude returned non-JSON for a batch of {len(batch)} headlines; "
                  f"leaving them as OTHER.", file=sys.stderr)
            yield start, batch_results
            continue

        if not isinstance(parsed, list):
            print(f"[warn] claude returned JSON but not a list for a batch of {len(batch)} headlines; "
                  f"leaving them as OTHER.", file=sys.stderr)
            yield start, batch_results
            continue

        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("i")
            event = entry.get("event")
            companies = entry.get("companies")
            if not isinstance(idx, int) or not (1 <= idx <= len(batch)) or event not in EVENT_TYPES:
                continue
            if not isinstance(companies, list):
                companies = []
            companies = [c for c in companies if isinstance(c, str) and c.strip()]
            pct_confidence = _coerce_pct(entry.get("pct_confidence"))
            batch_results[idx - 1] = {
                "event": event,
                "companies": companies,
                "profit_pct": _coerce_pct(entry.get("profit_pct")),
                "revenue_pct": _coerce_pct(entry.get("revenue_pct")),
                "order_is_material": bool(entry.get("order_is_material", False)),
                "pct_confidence": pct_confidence if pct_confidence is not None else 0,
            }

        yield start, batch_results


def classify_headlines_llm(titles, model=DEFAULT_MODEL, batch_size=BATCH_SIZE):
    """Non-incremental convenience wrapper -- returns the full list of
    {"event", "companies", "profit_pct", "revenue_pct", "order_is_material"},
    same length/order as titles. Prefer classify_headlines_llm_batches (or
    enrich's on_batch) for large title lists so progress isn't all-or-nothing."""
    results = [_default_result() for _ in titles]
    for start, batch_results in classify_headlines_llm_batches(titles, model=model, batch_size=batch_size):
        results[start:start + len(batch_results)] = batch_results
    return results


# LLM-extracted names should already be fairly clean -- a stricter cutoff than
# resolve_company()'s own default guards against weak/noisy grounding matches.
# 0.75 was too permissive: verified live, "Alphabet" (as in Google's parent --
# frequently mentioned in Indian tech/AI roundup headlines, not NSE-listed at
# all) scored exactly 0.75 against "ALPHAGEO (INDIA) LIMITED" (an unrelated
# real NSE company) purely from sharing an "ALPHA" prefix and similar length --
# a pure difflib.SequenceMatcher coincidence, nothing to do with the actual
# company. Same false-positive pattern hit every other major foreign tech
# name tested (Microsoft, Amazon, Apple, Meta, Nvidia, Tesla), all clustering
# in the 0.6-0.75 range. Raised to 0.8 -- confirmed this still clears every
# genuine match tested (Reliance Industries, Sun Pharma/Sun Pharmaceutical,
# HDFC Bank, Larsen & Toubro / "Larsen and Toubro Ltd" at 0.867, Tata
# Consultancy Services at exactly 0.8, State Bank of India, ITC, Bajaj
# Finance, Adani Ports all scored >= 0.8) while cleanly rejecting all of the
# above foreign-name collisions.
GROUNDING_CUTOFF = 0.8


def _ground_companies(names, headline):
    """Resolve each LLM-extracted name to a real, verified NSE company (never
    trust the LLM's own idea of a ticker). Deduped by symbol.

    Extra guard for single-word company names (the matched company's own
    listed name, after company_lookup.normalize() strips LTD/LIMITED/BANK/
    etc. suffixes) -- these are the ones most prone to a pure fuzzy-scoring
    coincidence in the first place (a short string leaves difflib.SequenceMatcher
    less to discriminate on -- see GROUNDING_CUTOFF's "Alphabet" -> ALPHAGEO
    notes). For those, additionally require that exact normalized word
    appears as a whole word in the ORIGINAL headline -- not just close to the
    LLM's guess -- before trusting the match. Multi-word names skip this
    (a fuzzy match across a longer name is already far more discriminating,
    and requiring an exact substring would reject legitimate abbreviated/
    reordered mentions like "Larsen and Toubro" for "LARSEN TOUBRO")."""
    headline_upper = headline.upper()
    grounded = {}
    for name in names:
        matches = company_lookup.resolve_company(name, top_n=1, cutoff=GROUNDING_CUTOFF)
        if not matches:
            continue
        m = matches[0]
        normalized_target = company_lookup.normalize(m["name"])
        if len(normalized_target.split()) == 1 and not re.search(rf"\b{re.escape(normalized_target)}\b", headline_upper):
            continue
        grounded[m["symbol"]] = {
            "symbol": m["symbol"], "name": m["name"],
            "instrument_key": m["instrument_key"], "match_type": "llm_extracted",
        }
    return list(grounded.values())


def enrich(items, model=DEFAULT_MODEL, batch_size=BATCH_SIZE, on_batch=None):
    """Mutates and returns items with: money, mentioned_companies, event_type,
    category, signal, profit_pct, revenue_pct, order_is_material,
    pct_low_confidence.

    profit_pct/revenue_pct are regex-extracted when the headline has a bare
    percentage near the relevant keyword (deterministic, wins over the LLM's
    own number), and fall back to the LLM's value otherwise -- see
    _combine_pct for how a single percentage's own confidence is decided.

    pct_low_confidence combines those per-field results asymmetrically (see
    _item_pct_confident), not just ANDed together:
        - both profit_pct and revenue_pct stated: confident only if BOTH are.
        - profit_pct only: confidence rests on profit_pct alone.
        - revenue_pct only (no profit_pct at all): always low-confidence,
          regardless of how sure regex/the LLM are about that revenue number
          -- a revenue-only headline isn't trusted enough to auto-buy on by
          itself.
    Downstream (trade_bot.py) uses this to decide whether an earnings-narrative
    candidate can auto-buy or must go to manual review.

    on_batch(batch_items), if given, is called after each batch is fully
    enriched -- lets callers persist/log progress incrementally instead of
    waiting for the whole list (a large backlog can take many minutes; without
    this, an interruption partway through loses everything classified so far)."""
    ensure_available()
    for it in items:
        it["money"] = extract_money(it["title"])

    titles = [it["title"] for it in items]
    for start, batch_results in classify_headlines_llm_batches(titles, model=model, batch_size=batch_size):
        batch_items = items[start:start + len(batch_results)]
        for it, r in zip(batch_items, batch_results):
            it["event_type"] = r["event"]
            it["category"], it["signal"] = EVENT_META[r["event"]]
            it["mentioned_companies"] = _ground_companies(r["companies"], it["title"])
            profit_pct, profit_confident = _combine_pct(
                extract_profit_pct_regex(it["title"]), r["profit_pct"], r["pct_confidence"])
            revenue_pct, revenue_confident = _combine_pct(
                extract_revenue_pct_regex(it["title"]), r["revenue_pct"], r["pct_confidence"])
            it["profit_pct"] = profit_pct
            it["revenue_pct"] = revenue_pct
            it["pct_low_confidence"] = not _item_pct_confident(
                profit_pct, profit_confident, revenue_pct, revenue_confident)
            it["order_is_material"] = r["order_is_material"]
        if on_batch:
            on_batch(batch_items)

    return items
