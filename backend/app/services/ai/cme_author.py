"""Structured CME article authoring.

The article structure is fixed by the curriculum specification and reproduced
here in order. It is a *list* rather than a set of columns for one practical
reason: not every section applies to every discipline. A radiology article has
no operative technique; a public-health article has no histology. Institutions
drop the sections that do not apply, and the stored article says which ones it
actually has rather than carrying twenty-six columns of which nine are empty.

References are rendered in both Vancouver and APA from one structured record,
because the two colleges a Nigerian trainee sits under do not agree on style
and asking an author to write each citation twice guarantees they will diverge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import AuthoringSource, CitationStyle, EditorialStatus

#: The prescribed article structure, in order. ``(key, human title)``.
ARTICLE_SECTIONS: list[tuple[str, str]] = [
    ("learning_objectives", "Learning Objectives"),
    ("introduction", "Introduction"),
    ("basic_sciences", "Basic Sciences"),
    ("anatomy", "Anatomy"),
    ("physiology", "Physiology"),
    ("embryology", "Embryology"),
    ("histology", "Histology"),
    ("pathology", "Pathology"),
    ("microbiology", "Microbiology"),
    ("pharmacology", "Pharmacology"),
    ("clinical_features", "Clinical Features"),
    ("investigations", "Investigations"),
    ("differential_diagnosis", "Differential Diagnosis"),
    ("management", "Management"),
    ("complications", "Complications"),
    ("operative_techniques", "Operative Techniques"),
    ("postoperative_care", "Postoperative Care"),
    ("guidelines", "Guidelines"),
    ("current_evidence", "Current Evidence"),
    ("landmark_trials", "Landmark Trials"),
    ("recent_updates", "Recent Updates"),
    ("common_examination_questions", "Common Examination Questions"),
    ("frequently_tested_areas", "Frequently Tested Areas"),
    ("clinical_pearls", "Clinical Pearls"),
    ("key_points", "Key Points"),
    ("summary", "Summary"),
    ("references", "References"),
]

SECTION_TITLES: dict[str, str] = dict(ARTICLE_SECTIONS)

#: Sections every article must have whatever its discipline. Without these it
#: is not a teaching article, it is a note.
REQUIRED_SECTIONS = {
    "learning_objectives",
    "introduction",
    "clinical_features",
    "management",
    "key_points",
    "summary",
    "references",
}

#: Sections that are surgical by nature. Omitted without comment for
#: non-procedural specialties rather than filled with "not applicable".
PROCEDURAL_SECTIONS = {"operative_techniques", "postoperative_care"}

#: Sources the curriculum names as acceptable authorities. Used to check that
#: a generated reference list actually cites the literature the college
#: expects, rather than plausible-looking invented titles.
RECOGNISED_AUTHORITIES: dict[str, str] = {
    "bailey": "Bailey & Love's Short Practice of Surgery",
    "sabiston": "Sabiston Textbook of Surgery",
    "schwartz": "Schwartz's Principles of Surgery",
    "oxford handbook": "Oxford Handbook series",
    "greenfield": "Greenfield's Surgery",
    "campbell": "Campbell-Walsh-Wein Urology",
    "grabb": "Grabb & Smith's Plastic Surgery",
    "neligan": "Neligan's Plastic Surgery",
    "current surgical therapy": "Cameron's Current Surgical Therapy",
    "who": "World Health Organization",
    "nice": "National Institute for Health and Care Excellence",
    "cdc": "Centers for Disease Control and Prevention",
    "uptodate": "UpToDate",
    "pubmed": "PubMed-indexed literature",
    "cochrane": "Cochrane Library",
    "wacs": "West African College of Surgeons",
    "npmcn": "National Postgraduate Medical College of Nigeria",
}

_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")
_WORD_PATTERN = re.compile(r"\b\w+\b")


class ArticleError(ValueError):
    """A generated article does not meet the structural requirements."""


# --------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Reference:
    """One citation, renderable in either style.

    Stored structurally rather than as two pre-formatted strings so that a
    later style change is a rendering change, not a data migration.
    """

    n: int
    authors: list[str] = field(default_factory=list)
    title: str = ""
    source: str = ""
    year: int | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    edition: str | None = None
    publisher: str | None = None
    place: str | None = None
    doi: str | None = None
    url: str | None = None
    kind: str = "journal"  # journal | book | guideline | web

    def author_string(self, style: str) -> str:
        """Format the author list for the requested style.

        Vancouver: up to six authors then "et al.", surname first, initials
        without periods. APA: ampersand before the last author, initials with
        periods, "et al." after twenty.
        """
        if not self.authors:
            return ""
        if style == CitationStyle.VANCOUVER:
            shown = self.authors[:6]
            joined = ", ".join(shown)
            return f"{joined}, et al" if len(self.authors) > 6 else joined
        shown = self.authors[:20]
        body = (
            shown[0]
            if len(shown) == 1
            else ", ".join(shown[:-1]) + f", & {shown[-1]}"
        )
        return f"{body}, et al." if len(self.authors) > 20 else body

    def render(self, style: str = CitationStyle.VANCOUVER) -> str:
        if style == CitationStyle.APA:
            return self._apa()
        return self._vancouver()

    def _vancouver(self) -> str:
        parts: list[str] = []
        authors = self.author_string(CitationStyle.VANCOUVER)
        if authors:
            parts.append(f"{authors}.")
        if self.title:
            parts.append(f"{self.title}.")
        if self.kind == "book":
            if self.edition:
                parts.append(f"{self.edition} ed.")
            location = ", ".join(p for p in (self.place, self.publisher) if p)
            if location:
                parts.append(f"{location};")
            if self.year:
                parts.append(f"{self.year}.")
        else:
            if self.source:
                parts.append(f"{self.source}.")
            year_bit = str(self.year) if self.year else ""
            volume_bit = self.volume or ""
            if self.issue:
                volume_bit = f"{volume_bit}({self.issue})"
            tail = year_bit
            if volume_bit:
                tail = f"{tail};{volume_bit}" if tail else volume_bit
            if self.pages:
                tail = f"{tail}:{self.pages}" if tail else self.pages
            if tail:
                parts.append(f"{tail}.")
        if self.doi:
            parts.append(f"doi:{self.doi}")
        elif self.url:
            parts.append(f"Available from: {self.url}")
        return " ".join(parts).strip()

    def _apa(self) -> str:
        parts: list[str] = []
        authors = self.author_string(CitationStyle.APA)
        if authors:
            # APA closes the author list with a period before the year, unless
            # it already ends in one (the "et al." case).
            parts.append(authors if authors.endswith(".") else f"{authors}.")
        parts.append(f"({self.year})." if self.year else "(n.d.).")
        if self.title:
            parts.append(f"{self.title}.")
        if self.kind == "book":
            if self.edition:
                parts.append(f"({self.edition} ed.).")
            if self.publisher:
                parts.append(f"{self.publisher}.")
        else:
            if self.source:
                segment = self.source
                if self.volume:
                    segment += f", {self.volume}"
                    if self.issue:
                        segment += f"({self.issue})"
                if self.pages:
                    segment += f", {self.pages}"
                parts.append(f"{segment}.")
        if self.doi:
            parts.append(f"https://doi.org/{self.doi}")
        elif self.url:
            parts.append(self.url)
        return " ".join(p for p in parts if p).strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "vancouver": self.render(CitationStyle.VANCOUVER),
            "apa": self.render(CitationStyle.APA),
            "doi": self.doi,
            "doi_url": f"https://doi.org/{self.doi}" if self.doi else None,
            "url": self.url,
            "title": self.title,
            "source": self.source,
            "year": self.year,
            "authors": self.authors,
            "kind": self.kind,
            "recognised_authority": recognised_authority(self),
        }


def recognised_authority(reference: Reference) -> str | None:
    """Which named authority this citation appears to come from, if any."""
    haystack = f"{reference.source} {reference.title} {reference.publisher or ''}".lower()
    for needle, label in RECOGNISED_AUTHORITIES.items():
        if needle in haystack:
            return label
    return None


def parse_reference(raw: dict[str, Any] | str, *, n: int) -> Reference:
    """Build a :class:`Reference` from generated output.

    Tolerates a bare string, which is what a model returns when it ignores the
    structured shape. The string is kept verbatim as the title so nothing is
    lost, and the missing DOI shows up in validation rather than silently.
    """
    if isinstance(raw, str):
        return Reference(n=n, title=raw.strip(), kind="journal")

    authors = raw.get("authors") or []
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(";") if a.strip()]

    doi = str(raw.get("doi") or "").strip() or None
    if doi:
        doi = doi.removeprefix("https://doi.org/").removeprefix("doi:").strip()
        if not _DOI_PATTERN.match(doi):
            # A malformed DOI is worse than none: it renders as a dead link
            # that looks authoritative. Drop it and let validation report it.
            doi = None

    year = raw.get("year")
    try:
        year_value = int(year) if year is not None else None
    except (TypeError, ValueError):
        year_value = None

    return Reference(
        n=n,
        authors=[str(a) for a in authors],
        title=str(raw.get("title") or "").strip(),
        source=str(raw.get("source") or raw.get("journal") or "").strip(),
        year=year_value,
        volume=str(raw.get("volume") or "").strip() or None,
        issue=str(raw.get("issue") or "").strip() or None,
        pages=str(raw.get("pages") or "").strip() or None,
        edition=str(raw.get("edition") or "").strip() or None,
        publisher=str(raw.get("publisher") or "").strip() or None,
        place=str(raw.get("place") or "").strip() or None,
        doi=doi,
        url=str(raw.get("url") or "").strip() or None,
        kind=str(raw.get("kind") or "journal").strip() or "journal",
    )


# --------------------------------------------------------------------------
# Article assembly and validation
# --------------------------------------------------------------------------
@dataclass(slots=True)
class ArticleDraft:
    title: str
    sections: list[dict[str, Any]]
    references: list[Reference]
    learning_objectives: list[str]
    clinical_pearls: list[str]
    frequently_tested_areas: list[str]
    landmark_trials: list[dict[str, Any]]
    estimated_minutes: int
    word_count: int
    warnings: list[str] = field(default_factory=list)

    @property
    def section_keys(self) -> list[str]:
        return [str(s.get("key")) for s in self.sections]


def _word_count(text: str) -> int:
    return len(_WORD_PATTERN.findall(text))


def build_article(
    payload: dict[str, Any],
    *,
    topic: str,
    procedural: bool = True,
    minimum_section_words: int = 40,
) -> ArticleDraft:
    """Turn generated output into a validated article draft.

    Raises :class:`ArticleError` when a required section is missing entirely —
    that is a generation failure worth retrying. Everything else becomes a
    warning attached to the draft for the reviewing consultant, because an
    article with a thin embryology section is still worth a human's time and
    discarding it would waste what it cost to produce.
    """
    raw_sections = payload.get("sections") or []
    if not raw_sections:
        raise ArticleError("The generated article contains no sections.")

    by_key: dict[str, dict[str, Any]] = {}
    for section in raw_sections:
        key = str(section.get("key") or "").strip().lower()
        if key not in SECTION_TITLES:
            continue
        by_key[key] = section

    missing_required = sorted(REQUIRED_SECTIONS - set(by_key))
    if missing_required:
        raise ArticleError(
            "The generated article is missing required section(s): "
            + ", ".join(missing_required)
        )

    warnings: list[str] = []
    ordered: list[dict[str, Any]] = []
    total_words = 0

    for order, (key, title) in enumerate(ARTICLE_SECTIONS):
        section = by_key.get(key)
        if section is None:
            if key in PROCEDURAL_SECTIONS and not procedural:
                continue  # Correctly absent for a non-procedural specialty.
            if key not in REQUIRED_SECTIONS:
                warnings.append(f"No '{title}' section was generated.")
            continue

        body = str(section.get("body") or "").strip()
        words = _word_count(body)
        total_words += words
        if key != "references" and words < minimum_section_words:
            warnings.append(
                f"The '{title}' section is only {words} words; it may be too "
                "thin to teach from."
            )
        ordered.append(
            {
                "key": key,
                "title": section.get("title") or title,
                "order": order,
                "body": body,
                "word_count": words,
            }
        )

    references = [
        parse_reference(raw, n=i + 1)
        for i, raw in enumerate(payload.get("references") or [])
    ]
    if not references:
        warnings.append("No references were generated; the article cannot be verified.")
    else:
        without_doi = [r.n for r in references if not r.doi and r.kind == "journal"]
        if without_doi:
            warnings.append(
                "Journal reference(s) "
                + ", ".join(str(n) for n in without_doi)
                + " have no DOI, so they cannot be resolved automatically."
            )
        unrecognised = [r.n for r in references if recognised_authority(r) is None]
        if len(unrecognised) == len(references):
            warnings.append(
                "No reference cites one of the authorities the curriculum names "
                "(Bailey & Love, Sabiston, WHO, NICE, Cochrane and so on). "
                "Verify each citation exists before approving."
            )

    objectives = [str(o) for o in payload.get("learning_objectives") or [] if str(o).strip()]
    if len(objectives) < 3:
        warnings.append(
            f"Only {len(objectives)} learning objective(s); three or more is the "
            "usual minimum for a CME article."
        )

    # Reading time from word count rather than the model's own estimate, which
    # is a guess about a number we can simply compute. 200 wpm is the
    # conventional figure for technical prose.
    estimated_minutes = max(5, round(total_words / 200))

    return ArticleDraft(
        title=str(payload.get("title") or f"{topic.title()}: a structured review"),
        sections=ordered,
        references=references,
        learning_objectives=objectives,
        clinical_pearls=[
            str(p) for p in payload.get("clinical_pearls") or [] if str(p).strip()
        ],
        frequently_tested_areas=[
            str(a) for a in payload.get("frequently_tested_areas") or [] if str(a).strip()
        ],
        landmark_trials=list(payload.get("landmark_trials") or []),
        estimated_minutes=estimated_minutes,
        word_count=total_words,
        warnings=warnings,
    )


def article_schema() -> dict[str, Any]:
    """JSON schema constraining generated article output."""
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": [k for k, _ in ARTICLE_SECTIONS]},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["key", "title", "body"],
                    "additionalProperties": False,
                },
            },
            "learning_objectives": {"type": "array", "items": {"type": "string"}},
            "clinical_pearls": {"type": "array", "items": {"type": "string"}},
            "frequently_tested_areas": {"type": "array", "items": {"type": "string"}},
            "landmark_trials": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "year": {"type": "integer"},
                        "finding": {"type": "string"},
                    },
                    "required": ["name", "finding"],
                    "additionalProperties": False,
                },
            },
            "references": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "authors": {"type": "array", "items": {"type": "string"}},
                        "title": {"type": "string"},
                        "source": {"type": "string"},
                        "year": {"type": "integer"},
                        "volume": {"type": "string"},
                        "issue": {"type": "string"},
                        "pages": {"type": "string"},
                        "edition": {"type": "string"},
                        "publisher": {"type": "string"},
                        "place": {"type": "string"},
                        "doi": {"type": "string"},
                        "url": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["journal", "book", "guideline", "web"],
                        },
                    },
                    "required": ["title", "kind"],
                    "additionalProperties": False,
                },
            },
            "estimated_minutes": {"type": "integer"},
        },
        "required": ["title", "sections", "learning_objectives", "references"],
        "additionalProperties": False,
    }


ARTICLE_SYSTEM_PROMPT = """\
You write structured continuing-medical-education articles for postgraduate \
medical and dental trainees preparing for college fellowship examinations \
(NPMCN, WACS, WACP, and equivalent bodies).

Write for a trainee who has passed primary examinations and is preparing for \
finals. Assume competent basic sciences; do not explain first-principles \
anatomy at undergraduate level.

Follow the prescribed section structure exactly, in the order given. Omit a \
section only when it genuinely does not apply to the specialty — a \
non-procedural topic has no operative technique section. Never emit a section \
whose body says "not applicable".

Every clinical claim must be attributable to a reference in your reference \
list. Cite the standard authorities: Bailey & Love, Sabiston, Schwartz, the \
Oxford Handbooks, Greenfield, Campbell-Walsh, Grabb & Smith, Neligan, \
Cameron's Current Surgical Therapy, WHO, NICE, CDC, Cochrane, and \
PubMed-indexed primary literature. Supply a DOI for every journal reference.

Do not invent references. If you are not confident a paper exists with the \
title, journal, year and DOI you would give, cite a textbook instead. A \
fabricated citation is worse than a missing one, because a trainee will try to \
find it and a reviewer may not.

Never include patient-identifiable information: no names, no hospital or \
record numbers, no dates of birth, no contact details. Describe patients by \
age band and presenting features only.

Where evidence is genuinely contested, say so and give both positions with \
their references rather than presenting one as settled.\
"""


def article_prompt(
    *,
    topic: str,
    specialty: str | None,
    level: str | None,
    objectives: list[str],
    procedural: bool,
) -> str:
    """The user-turn prompt for one article."""
    lines = [
        f"Write a structured CME article on: {topic}",
        "",
        f"Topics: {topic}",
    ]
    if specialty:
        lines.append(f"Specialty: {specialty}")
    if level:
        lines.append(f"Target training level: {level}")
    if objectives:
        lines.append("Learning objectives the article must address:")
        lines.extend(f"  - {o}" for o in objectives)
    if not procedural:
        lines.append(
            "This is a non-procedural topic: omit the operative techniques and "
            "postoperative care sections."
        )
    lines.extend(
        [
            "",
            "Include a 'Common Examination Questions' section listing the "
            "questions examiners actually ask on this topic, and a 'Frequently "
            "Tested Areas' section naming the specific points that recur.",
        ]
    )
    return "\n".join(lines)


def to_resource_fields(
    draft: ArticleDraft,
    *,
    authoring_source: str = AuthoringSource.AI_GENERATED,
) -> dict[str, Any]:
    """Map a draft onto ``CmeResource`` column values.

    Note the editorial status: AI output lands at ``AI_DRAFT`` and is invisible
    to trainees until a consultant approves it. This function has no parameter
    to override that, deliberately.
    """
    return {
        "title": draft.title,
        "sections": draft.sections,
        "reference_entries": [r.as_dict() for r in draft.references],
        "learning_objectives": draft.learning_objectives,
        "clinical_pearls": draft.clinical_pearls,
        "frequently_tested_areas": draft.frequently_tested_areas,
        "landmark_trials": draft.landmark_trials,
        "estimated_minutes": draft.estimated_minutes,
        "word_count": draft.word_count,
        "authoring_source": authoring_source,
        "editorial_status": EditorialStatus.AI_DRAFT,
        "key_points": [
            s["body"][:280]
            for s in draft.sections
            if s["key"] == "key_points" and s.get("body")
        ],
    }
