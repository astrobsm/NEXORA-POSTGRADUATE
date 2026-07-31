"""Automated item quality checks, and duplicate detection.

Every generated item passes through here before it can become a question, and
every check is a documented item-writing rule rather than a taste judgement.
The reference is the NBME item-writing manual (Paniagua & Swygert, *Constructing
Written Test Questions for the Basic and Clinical Sciences*), which is what
NPMCN, WACS and the Royal Colleges all draw on.

Two properties matter more than the individual rules:

**Every rejection names its reason.** A pipeline that reports "17 items
rejected" is useless; one that reports "9 lacked per-distractor rationales, 5
used absolute terms, 3 were near-duplicates of existing items" tells the
department what to fix in the prompt.

**Nothing here can pass an item.** A clean sheet means the item is fit to go to
a human reviewer, not fit to be served. Publication needs a person.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import QualityCheck

#: House style: five options, one key.
EXPECTED_OPTION_COUNT = 5
#: A stem shorter than this cannot carry a clinical vignette.
MINIMUM_STEM_CHARS = 60
#: A stem longer than this is testing reading speed.
MAXIMUM_STEM_CHARS = 1800
#: An explanation shorter than this explains nothing.
MINIMUM_EXPLANATION_CHARS = 80
#: Below this many references an item cannot be checked against anything.
MINIMUM_REFERENCES = 1

#: Absolute qualifiers. Candidates learn that options containing them are
#: almost always wrong, so their presence gives away the answer without
#: testing knowledge.
ABSOLUTE_TERMS = re.compile(
    r"\b(always|never|all|none|every|invariably|exclusively|without exception)\b",
    re.IGNORECASE,
)

#: Non-answers that test reading comprehension rather than clinical knowledge.
NON_ANSWER_OPTIONS = re.compile(
    r"^\s*(all|none|both|any)\s+of\s+the\s+(above|following|these)\s*\.?\s*$",
    re.IGNORECASE,
)

#: Vague frequency terms that mean different things to different candidates.
VAGUE_TERMS = re.compile(
    r"\b(usually|frequently|occasionally|rarely|often|sometimes)\b", re.IGNORECASE
)

#: Patterns that look like patient-identifiable data. Deliberately broad: a
#: false positive costs one regenerated item, a false negative puts an
#: identifier into a bank that will be exported, printed and emailed. The
#: platform stores no patient-identifiable data by design and generated content
#: is not an exception.
IDENTIFIER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("hospital or record number", re.compile(r"\b(?:MRN|NHS|RN|HN)[-\s:]?\d{4,}\b", re.I)),
    ("record number", re.compile(r"\b(?:hospital|record|patient)\s+(?:number|no\.?|id)\s*[:\-]?\s*\S+", re.I)),
    ("long numeric identifier", re.compile(r"\b\d{7,}\b")),
    ("full date of birth", re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")),
    # Matches "Dr Okafor", "Mrs A Okafor" and "Prof. A. B. Okafor" alike. The
    # optional initials group is load-bearing: without it "Mrs A Okafor" slips
    # through, because a bare initial is not [A-Z][a-z]+.
    (
        "named individual with title",
        re.compile(
            r"\b(?:Mr|Mrs|Miss|Ms|Dr|Prof|Sir|Chief|Alhaji|Hajia)\.?\s+"
            r"(?:[A-Z]\.?\s+){0,3}[A-Z][a-z]+"
        ),
    ),
    ("email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("telephone number", re.compile(r"\+?\d[\d\s-]{9,}\d")),
]

#: Words carrying no discriminating power, dropped before shingling so that
#: "management of acute appendicitis" and "the management of an acute
#: appendicitis" hash alike.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for",
    "from", "had", "has", "have", "he", "her", "his", "in", "is", "it", "its",
    "of", "on", "or", "she", "that", "the", "their", "there", "they", "this",
    "to", "was", "were", "what", "which", "who", "will", "with", "you", "your",
}

#: Long but ubiquitous clinical vocabulary. Excluded from the cueing check
#: because these words appear in almost every stem and almost every option, so
#: treating them as cues flags a large share of perfectly sound items. A cue is
#: a *distinctive* shared word — "cholecystectomy" in both stem and key is a
#: cue; "examination" is not.
CLINICAL_COMMON_WORDS = {
    "examination", "investigation", "investigations", "management", "treatment",
    "treatments", "assessment", "diagnosis", "diagnostic", "presentation",
    "presenting", "clinical", "patients", "symptoms", "condition", "conditions",
    "following", "appropriate", "immediate", "immediately", "consistent",
    "history", "findings", "features", "suspected", "underlying", "identified",
    "described", "reasonable", "urgently", "surgical", "medical", "hospital",
    "operative", "procedure", "procedures", "commence", "consider",
}

#: Consecutive words per shingle. Two, not the more usual three.
#:
#: Measured on paraphrase pairs (same case, reworded: tense changed, an article
#: inserted, a synonym swapped) against genuinely distinct items on the same
#: topic. Trigrams score real paraphrase at 0.25-0.37 and distinct items at
#: 0.00 — the separation exists but the paraphrase band sits so low that any
#: threshold catching it also catches unrelated items on a longer stem.
#: Bigrams score paraphrase at 0.50-0.75 and distinct items at 0.00-0.14.
SHINGLE_SIZE = 2

#: The overlap (containment) coefficient at or above which two items are
#: treated as duplicates.
#:
#: Containment rather than Jaccard because Jaccard penalises length
#: differences twice over: a reworded stem with three extra words shrinks the
#: intersection *and* grows the union. Two items that say the same thing at
#: different lengths are still duplicates.
#:
#: 0.45 sits between the measured bands with roughly threefold margin on each
#: side. Adjust it against real data, not intuition — and if you do, re-run
#: ``TestDuplicateDetection``, which pins both sides of the boundary.
DUPLICATE_THRESHOLD = 0.45

#: Below this many shingles, containment is not meaningful: a four-word stem
#: is trivially "contained" in a long one. Such items fall back to exact
#: matching alone.
MINIMUM_SHINGLES_FOR_SIMILARITY = 4


@dataclass(slots=True)
class CheckResult:
    check: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "passed": self.passed, "detail": self.detail}


@dataclass(slots=True)
class QualityReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    @property
    def summary(self) -> str:
        if self.passed:
            return "All checks passed."
        return "; ".join(f"{r.check}: {r.detail}" for r in self.failures)

    def as_list(self) -> list[dict[str, Any]]:
        return [r.as_dict() for r in self.results]


# --------------------------------------------------------------------------
# Normalisation and fingerprinting
# --------------------------------------------------------------------------
def normalise(text: str) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace.

    Unicode normalisation matters more than it looks: an em dash and a hyphen,
    or a curly and a straight apostrophe, are different bytes and would
    otherwise defeat exact-duplicate detection entirely.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = stripped.casefold()
    cleaned = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def content_hash(stem: str, options: list[dict[str, Any]] | None = None) -> str:
    """A stable fingerprint for exact-duplicate detection.

    Covers the stem and the *set* of option texts, so reordering the options
    does not disguise a duplicate — which is the most obvious way an item would
    otherwise slip past.
    """
    import hashlib

    parts = [normalise(stem)]
    if options:
        parts.extend(sorted(normalise(str(o.get("text", ""))) for o in options))
    return hashlib.sha256("␟".join(parts).encode()).hexdigest()[:48]


def shingles(text: str, *, size: int = SHINGLE_SIZE) -> list[str]:
    """Overlapping word n-grams with stopwords removed."""
    words = [w for w in normalise(text).split() if w not in STOPWORDS]
    if len(words) < size:
        return [" ".join(words)] if words else []
    return [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]


def similarity(left: list[str], right: list[str]) -> float:
    """Overlap coefficient between two shingle sets.

    ``|A ∩ B| / min(|A|, |B|)`` rather than Jaccard's ``|A ∩ B| / |A ∪ B|``.
    See :data:`DUPLICATE_THRESHOLD` for the measurement behind that choice.

    Returns 0.0 when either side is too short to compare, because a four-word
    stem is trivially contained in a long one and would otherwise score 1.0
    against anything that happened to include it.
    """
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    if min(len(a), len(b)) < MINIMUM_SHINGLES_FOR_SIMILARITY:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# --------------------------------------------------------------------------
# Item checks
# --------------------------------------------------------------------------
def _check_option_count(item: dict[str, Any]) -> CheckResult:
    options = item.get("options") or []
    ok = len(options) == EXPECTED_OPTION_COUNT
    return CheckResult(
        QualityCheck.OPTION_COUNT,
        ok,
        "" if ok else f"{len(options)} options; house style is {EXPECTED_OPTION_COUNT}.",
    )


def _check_single_key(item: dict[str, Any]) -> CheckResult:
    options = item.get("options") or []
    keys = [o for o in options if o.get("is_correct")]
    ok = len(keys) == 1
    return CheckResult(
        QualityCheck.EXACTLY_ONE_CORRECT,
        ok,
        ""
        if ok
        else f"{len(keys)} options marked correct; single best answer requires one.",
    )


def _check_absolute_terms(item: dict[str, Any]) -> CheckResult:
    offenders = [
        str(o.get("key"))
        for o in item.get("options") or []
        if ABSOLUTE_TERMS.search(str(o.get("text", "")))
    ]
    ok = not offenders
    return CheckResult(
        QualityCheck.NO_ABSOLUTE_TERMS,
        ok,
        ""
        if ok
        else (
            f"Option(s) {', '.join(offenders)} contain an absolute term "
            "(always/never/all/none), which signals the answer."
        ),
    )


def _check_non_answers(item: dict[str, Any]) -> CheckResult:
    offenders = [
        str(o.get("key"))
        for o in item.get("options") or []
        if NON_ANSWER_OPTIONS.match(str(o.get("text", "")))
    ]
    ok = not offenders
    return CheckResult(
        QualityCheck.NO_ALL_OF_THE_ABOVE,
        ok,
        ""
        if ok
        else (
            f"Option(s) {', '.join(offenders)} are 'all/none of the above', which "
            "test reading rather than knowledge."
        ),
    )


def _check_homogeneous(item: dict[str, Any]) -> CheckResult:
    """Options should be the same kind of thing and roughly the same length.

    A markedly longer option is the classic tell: writers elaborate the correct
    answer and leave the distractors terse.
    """
    options = item.get("options") or []
    lengths = [len(str(o.get("text", ""))) for o in options if o.get("text")]
    if len(lengths) < 2:
        return CheckResult(QualityCheck.HOMOGENEOUS_OPTIONS, True)

    longest = max(lengths)
    median = sorted(lengths)[len(lengths) // 2]
    key = next((o for o in options if o.get("is_correct")), None)
    key_length = len(str(key.get("text", ""))) if key else 0

    # Only a fault when the *correct* option is the long one — that is what
    # gives the answer away. A long distractor is merely untidy.
    ok = not (key_length == longest and median > 0 and longest > median * 1.8)
    return CheckResult(
        QualityCheck.HOMOGENEOUS_OPTIONS,
        ok,
        ""
        if ok
        else (
            f"The correct option is {longest} characters against a median of "
            f"{median}; length gives the answer away."
        ),
    )


def _check_stem_length(item: dict[str, Any]) -> CheckResult:
    stem = str(item.get("stem") or "")
    length = len(stem)
    if length < MINIMUM_STEM_CHARS:
        return CheckResult(
            QualityCheck.STEM_LENGTH,
            False,
            f"Stem is {length} characters; too short to carry a clinical vignette.",
        )
    if length > MAXIMUM_STEM_CHARS:
        return CheckResult(
            QualityCheck.STEM_LENGTH,
            False,
            f"Stem is {length} characters; above {MAXIMUM_STEM_CHARS} it tests "
            "reading speed.",
        )
    return CheckResult(QualityCheck.STEM_LENGTH, True)


def _check_no_clue(item: dict[str, Any]) -> CheckResult:
    """The stem must not repeat a distinctive phrase from the correct option.

    Word repetition between stem and key is a well-documented cueing fault:
    candidates who do not know the answer pick the option that echoes the stem.
    """
    def distinctive(text: str) -> set[str]:
        return {
            w
            for w in normalise(text).split()
            if len(w) > 6 and w not in CLINICAL_COMMON_WORDS
        }

    stem_words = distinctive(str(item.get("stem", "")))
    key = next((o for o in item.get("options") or [] if o.get("is_correct")), None)
    if key is None or not stem_words:
        return CheckResult(QualityCheck.NO_CLUE_IN_STEM, True)

    key_words = distinctive(str(key.get("text", "")))
    others: set[str] = set()
    for option in item.get("options") or []:
        if option.get("is_correct"):
            continue
        others |= distinctive(str(option.get("text", "")))

    # Only distinctive words that appear in the key and in *no* distractor.
    cues = (stem_words & key_words) - others
    ok = not cues
    return CheckResult(
        QualityCheck.NO_CLUE_IN_STEM,
        ok,
        ""
        if ok
        else (
            f"The stem repeats {', '.join(sorted(cues))} from the correct option "
            "only, which cues the answer."
        ),
    )


def _check_explanation(item: dict[str, Any]) -> CheckResult:
    explanation = str(item.get("explanation") or "").strip()
    ok = len(explanation) >= MINIMUM_EXPLANATION_CHARS
    return CheckResult(
        QualityCheck.EXPLANATION_PRESENT,
        ok,
        ""
        if ok
        else (
            f"Explanation is {len(explanation)} characters; feedback needs at "
            f"least {MINIMUM_EXPLANATION_CHARS}."
        ),
    )


def _check_distractor_rationales(item: dict[str, Any]) -> CheckResult:
    """Every option needs its own reason, correct and incorrect alike.

    This is what makes post-examination review teach anything. Without it a
    candidate learns which letter was right, not why their choice was wrong.
    """
    missing = [
        str(o.get("key"))
        for o in item.get("options") or []
        if not str(o.get("rationale") or "").strip()
    ]
    ok = not missing
    return CheckResult(
        QualityCheck.DISTRACTOR_RATIONALES_PRESENT,
        ok,
        "" if ok else f"Option(s) {', '.join(missing)} have no rationale.",
    )


def _check_references(item: dict[str, Any]) -> CheckResult:
    references = [r for r in (item.get("references") or []) if str(r).strip()]
    ok = len(references) >= MINIMUM_REFERENCES
    return CheckResult(
        QualityCheck.REFERENCES_PRESENT,
        ok,
        "" if ok else "No reference given; the item cannot be verified.",
    )


def _check_curriculum_mapping(item: dict[str, Any]) -> CheckResult:
    missing = [
        field_name
        for field_name in ("topic", "blueprint_category", "difficulty_band")
        if not str(item.get(field_name) or "").strip()
    ]
    ok = not missing
    return CheckResult(
        QualityCheck.CURRICULUM_MAPPED,
        ok,
        "" if ok else f"Missing curriculum mapping: {', '.join(missing)}.",
    )


def _check_no_identifiers(item: dict[str, Any]) -> CheckResult:
    """No patient-identifiable data anywhere in the item.

    The platform stores none by design; generated content is not an exception,
    and a bank is exported and printed far more often than a database row.
    """
    haystack = " ".join(
        [
            str(item.get("stem") or ""),
            str(item.get("lead_in") or ""),
            str(item.get("explanation") or ""),
            *[str(o.get("text") or "") for o in item.get("options") or []],
            *[str(o.get("rationale") or "") for o in item.get("options") or []],
        ]
    )
    for label, pattern in IDENTIFIER_PATTERNS:
        match = pattern.search(haystack)
        if match:
            return CheckResult(
                QualityCheck.NO_PATIENT_IDENTIFIERS,
                False,
                f"Possible {label} found: {match.group(0)[:40]!r}.",
            )
    return CheckResult(QualityCheck.NO_PATIENT_IDENTIFIERS, True)


ITEM_CHECKS = (
    _check_option_count,
    _check_single_key,
    _check_absolute_terms,
    _check_non_answers,
    _check_homogeneous,
    _check_stem_length,
    _check_no_clue,
    _check_explanation,
    _check_distractor_rationales,
    _check_references,
    _check_curriculum_mapping,
    _check_no_identifiers,
)


def check_item(item: dict[str, Any]) -> QualityReport:
    """Run every automated check against one generated item."""
    return QualityReport(results=[check(item) for check in ITEM_CHECKS])


def advisory_warnings(item: dict[str, Any]) -> list[str]:
    """Style notes that inform a reviewer without blocking an item.

    Kept separate from the checks on purpose. Vague frequency terms and a
    missing lead-in are worth a reviewer's attention but are not defects, and
    rejecting on them would burn generation budget on matters of taste.
    """
    warnings: list[str] = []
    stem = str(item.get("stem") or "")
    if VAGUE_TERMS.search(stem):
        warnings.append(
            "The stem uses a vague frequency term (usually, often, rarely); "
            "these mean different things to different candidates."
        )
    if not str(item.get("lead_in") or "").strip():
        warnings.append(
            "No lead-in question. A candidate should be able to answer from the "
            "stem alone, before reading the options."
        )
    confidence = item.get("ai_confidence")
    if isinstance(confidence, int | float) and confidence < 0.6:
        warnings.append(
            f"The generator reported low confidence ({confidence:.2f}); worth "
            "closer review."
        )
    return warnings


# --------------------------------------------------------------------------
# Duplicate detection
# --------------------------------------------------------------------------
@dataclass(slots=True)
class DuplicateMatch:
    existing_id: str | None
    score: float
    kind: str  # "exact" | "near"


def find_duplicate(
    *,
    item_hash: str,
    item_shingles: list[str],
    known_hashes: dict[str, str],
    known_shingles: dict[str, list[str]],
    threshold: float = DUPLICATE_THRESHOLD,
) -> DuplicateMatch | None:
    """Match a candidate item against the live bank and the current batch.

    Exact first, because it is a dictionary lookup over the whole bank. Only
    then the O(n) shingle comparison, which is why the caller narrows
    ``known_shingles`` to the relevant topic rather than passing a hundred
    thousand items.
    """
    if item_hash in known_hashes:
        return DuplicateMatch(known_hashes[item_hash], 1.0, "exact")

    best: DuplicateMatch | None = None
    for existing_id, existing in known_shingles.items():
        score = similarity(item_shingles, existing)
        if score >= threshold and (best is None or score > best.score):
            best = DuplicateMatch(existing_id, round(score, 4), "near")
    return best
