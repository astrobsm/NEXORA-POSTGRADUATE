"""The language-model seam.

One interface, two implementations:

* :class:`AnthropicProvider` calls Claude through the official SDK.
* :class:`MockProvider` produces deterministic, structurally valid output with
  no network and no key.

The mock is not a stub for tests to tolerate — it is what lets the entire
generation workflow (retrieve, generate, validate, deduplicate, balance,
review, release) be demonstrated and regression-tested by anyone who checks the
repository out, including in CI where no API key exists. Its output is
deliberately *plausible but obviously synthetic*, so nobody can mistake a mock
run for real content: every stem it writes says so.

Anthropic API specifics worth knowing when reading this file:

* ``claude-opus-5`` is the model id, complete as written. Never append a date.
* Thinking is on by default on this model. ``max_tokens`` caps thinking *plus*
  visible output, which is why the ceiling here is generous.
* Sampling parameters (``temperature``, ``top_p``, ``top_k``) are rejected with
  a 400. Behaviour is steered by the prompt, not by a temperature dial.
* Requests are streamed. A batch of ten fully-explained items is a long
  response and a non-streaming call risks an HTTP timeout.
* ``stop_reason`` is checked before ``content`` is read. Safety classifiers can
  decline a request and return HTTP 200 with an empty content list; code that
  indexes ``content[0]`` unconditionally breaks on exactly the clinical topics
  this platform is built for.
* The system prompt and the retrieved knowledge are cached, because they are
  identical across every batch in a job and are the bulk of the input.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import settings

#: Beta flag for the server-side refusal fallback in its scalar "default" form.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AiUnavailable(RuntimeError):
    """AI generation was requested but cannot run.

    Raised with an actionable reason — a missing key, a disabled feature flag,
    an uninstalled SDK — rather than surfacing an import error or a 401 from
    deep inside a pipeline stage.
    """


class AiRefused(RuntimeError):
    """The model declined the request on safety grounds.

    Carries the category so the caller can distinguish "rephrase this" from
    "this topic is out of scope for automated authoring".
    """

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        self.category = category


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=(
                self.cache_read_input_tokens + other.cache_read_input_tokens
            ),
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
        )

    def cost_usd(
        self,
        *,
        input_per_mtok: float | None = None,
        output_per_mtok: float | None = None,
    ) -> float:
        """Estimated spend.

        Cache reads bill at roughly a tenth of the input rate and cache writes
        at about 1.25x. Ignoring both would overstate the cost of a job that
        reuses one knowledge context across twenty batches by a wide margin,
        and the whole point of recording cost is that a department can trust it.
        """
        rate_in = (
            input_per_mtok
            if input_per_mtok is not None
            else settings.ai_input_cost_per_mtok
        )
        rate_out = (
            output_per_mtok
            if output_per_mtok is not None
            else settings.ai_output_cost_per_mtok
        )
        total = (
            self.input_tokens * rate_in
            + self.cache_read_input_tokens * rate_in * 0.1
            + self.cache_creation_input_tokens * rate_in * 1.25
            + self.output_tokens * rate_out
        )
        return round(total / 1_000_000, 6)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "estimated_cost_usd": self.cost_usd(),
        }


@dataclass(slots=True)
class ProviderResponse:
    data: Any
    usage: Usage
    model: str
    provider: str
    #: True when the model declined. ``data`` is then ``None``.
    refused: bool = False
    refusal_category: str | None = None
    #: Populated when a fallback model served the request.
    served_by_fallback: bool = False
    notes: list[str] = field(default_factory=list)


class AiProvider(Protocol):
    """What the generation pipeline needs from a language model."""

    name: str
    model: str

    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int | None = None,
        cached_context: str | None = None,
    ) -> ProviderResponse:
        """Return JSON validated against ``schema``.

        ``cached_context`` is large, stable material — retrieved guidelines,
        curriculum extracts — that is identical across calls in one job. It is
        placed ahead of the varying prompt and marked cacheable.
        """
        ...


# ==========================================================================
# Anthropic
# ==========================================================================
class AnthropicProvider:
    """Claude, through the official Anthropic SDK."""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        base_url: str | None = None,
        enable_fallbacks: bool | None = None,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise AiUnavailable(
                "The 'anthropic' package is not installed. Install the AI extra:\n"
                "    pip install 'anthropic>=0.69'\n"
                "or leave RTC_ENABLE_AI_GENERATION unset to use the offline "
                "provider."
            ) from exc

        self._anthropic = anthropic
        self.model = model or settings.ai_model
        self.effort = effort or settings.ai_effort
        self.enable_fallbacks = (
            settings.ai_enable_fallbacks if enable_fallbacks is None else enable_fallbacks
        )

        key = api_key or settings.ai_api_key
        client_kwargs: dict[str, Any] = {}
        if key:
            client_kwargs["api_key"] = key
        if base_url or settings.ai_provider_base_url:
            client_kwargs["base_url"] = base_url or settings.ai_provider_base_url
        # With no explicit key the SDK still resolves credentials from the
        # environment or a stored profile, so a bare client is valid. Let it
        # try, and let a 401 be the thing that says otherwise.
        self._client = anthropic.Anthropic(**client_kwargs)

    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int | None = None,
        cached_context: str | None = None,
    ) -> ProviderResponse:
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cached_context:
            # The breakpoint goes on the last stable block. Everything before it
            # is byte-identical across the batches of one job, so the second and
            # subsequent calls read the knowledge context from cache at about a
            # tenth of the price instead of re-sending it.
            system_blocks.append(
                {
                    "type": "text",
                    "text": cached_context,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        else:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or settings.ai_max_output_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": prompt}],
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }

        try:
            if self.enable_fallbacks:
                # Benign clinical and pharmacology content occasionally trips a
                # safety classifier. Without a fallback the request simply
                # stops; with one the API re-runs it on a suitable model inside
                # the same call.
                with self._client.beta.messages.stream(
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                    **request,
                ) as stream:
                    message = stream.get_final_message()
            else:
                with self._client.messages.stream(**request) as stream:
                    message = stream.get_final_message()
        except self._anthropic.APIStatusError as exc:
            raise AiUnavailable(
                f"The model provider returned {exc.status_code}: {exc.message}"
            ) from exc
        except self._anthropic.APIConnectionError as exc:
            raise AiUnavailable(
                "Could not reach the model provider. Check network access from "
                "the application host."
            ) from exc

        usage = Usage(
            input_tokens=getattr(message.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(message.usage, "output_tokens", 0) or 0,
            cache_read_input_tokens=(
                getattr(message.usage, "cache_read_input_tokens", 0) or 0
            ),
            cache_creation_input_tokens=(
                getattr(message.usage, "cache_creation_input_tokens", 0) or 0
            ),
        )

        # Checked before content is touched. A declined request returns HTTP
        # 200 with an empty content list, and indexing into it would raise an
        # IndexError that says nothing about what actually happened.
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            return ProviderResponse(
                data=None,
                usage=usage,
                model=getattr(message, "model", self.model),
                provider=self.name,
                refused=True,
                refusal_category=getattr(details, "category", None),
                notes=["The model declined this request on safety grounds."],
            )

        served_by_fallback = any(
            getattr(block, "type", None) == "fallback" for block in message.content
        )
        text = next(
            (b.text for b in message.content if getattr(b, "type", None) == "text"),
            "",
        )
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AiUnavailable(
                "The model returned output that was not valid JSON despite a "
                f"schema being enforced: {exc}"
            ) from exc

        notes: list[str] = []
        if message.stop_reason == "max_tokens":
            # Worth surfacing: a truncated batch produces items missing their
            # explanations, which the quality gate will reject anyway — but the
            # reason should say "the response was cut off", not "bad item".
            notes.append(
                "The response hit max_tokens and may be truncated. Reduce the "
                "batch size or raise RTC_AI_MAX_OUTPUT_TOKENS."
            )

        return ProviderResponse(
            data=data,
            usage=usage,
            model=getattr(message, "model", self.model),
            provider=self.name,
            served_by_fallback=served_by_fallback,
            notes=notes,
        )


# ==========================================================================
# Deterministic offline provider
# ==========================================================================
#: Clinical scaffolding the mock draws on. Enough variety that generated items
#: differ from one another and duplicate detection has something to detect,
#: without pretending to be real clinical content.
_MOCK_PRESENTATIONS = [
    "a 34-year-old presenting with a 6-hour history of severe abdominal pain",
    "a 58-year-old with progressive dysphagia over three months",
    "a 27-year-old in the emergency department after a road traffic collision",
    "a 71-year-old with a painful, cold lower limb of sudden onset",
    "a 45-year-old with intermittent right upper quadrant pain after meals",
    "a neonate with bilious vomiting on the second day of life",
    "a 63-year-old with painless macroscopic haematuria",
    "a 19-year-old with a rapidly enlarging neck swelling",
]

_MOCK_ACTIONS = [
    "Arrange urgent contrast-enhanced computed tomography",
    "Commence intravenous fluid resuscitation and reassess",
    "Refer immediately for surgical exploration",
    "Request an erect chest radiograph",
    "Start broad-spectrum antimicrobial therapy after cultures",
    "Perform a focused bedside ultrasound examination",
    "Admit for observation with serial examination",
    "Discharge with outpatient follow-up in two weeks",
]

#: Varying clinical detail. The mock composes a stem from several of these so
#: that its items genuinely differ from one another. That matters: with a fixed
#: template the duplicate detector correctly flags every generated item as a
#: near-duplicate of the last, and the pipeline's tests would then be measuring
#: the test double rather than the pipeline.
#: Note the vocabulary discipline: none of these share a distinctive word with
#: ``_MOCK_ACTIONS``. They deliberately avoid "radiograph", "ultrasound",
#: "tomography" and "examination", because a word appearing in both the stem
#: and the correct option is a genuine cueing fault — the quality gate catches
#: it, correctly, and a mock that tripped its own gate would make
#: ``defect_rate=0`` a lie.
_MOCK_FINDINGS = [
    "Observations show a temperature of 38.4 degrees and a heart rate of 112",
    "The abdomen is rigid with absent bowel sounds throughout",
    "Haemoglobin has fallen from 12.1 to 8.4 over six hours",
    "There is guarding across all four quadrants with rebound tenderness",
    "Serum lactate is 4.8 and urine output has been 10 millilitres per hour",
    "The white cell count is 21.6 with a marked neutrophilia",
    "Blood pressure is 86 over 48 and unresponsive to a fluid challenge",
    "Bowel habit has altered markedly over the preceding three months",
]

_MOCK_CONTEXTS = [
    "The patient underwent laparotomy for a similar problem four years ago",
    "There is a background of poorly controlled type 2 diabetes",
    "Regular medication includes an anticoagulant taken that morning",
    "The referral came from a district facility eleven hours away",
    "No theatre is free for the next six hours",
    "The patient has declined blood products on religious grounds",
    "This is the third such episode within the current year",
    "A relative reports a strong family history of malignancy",
]

_MOCK_REFERENCES = [
    "Bailey & Love's Short Practice of Surgery. 28th ed. Boca Raton: CRC Press; 2023.",
    "Sabiston Textbook of Surgery. 21st ed. Philadelphia: Elsevier; 2022.",
    "Schwartz's Principles of Surgery. 11th ed. New York: McGraw Hill; 2019.",
    "World Health Organization. Surgical care at the district hospital. Geneva: WHO; 2020.",
    "National Institute for Health and Care Excellence. Clinical guideline NG. London: NICE; 2023.",
]


class MockProvider:
    """A deterministic provider for offline development, CI and demonstration.

    Given the same prompt it returns the same output, which is what makes the
    pipeline's tests meaningful. It produces structurally complete items —
    five options, one key, per-distractor rationales, references, curriculum
    mapping — so every downstream quality check is genuinely exercised rather
    than skipped.

    A deliberate detail: roughly one item in seven is generated *malformed*,
    cycling through the specific defects the quality gate exists to catch. A
    mock that only ever emits perfect items would let a broken validator pass
    its tests forever.
    """

    name = "mock"
    model = "offline-deterministic"

    def __init__(self, *, defect_rate: int = 7) -> None:
        #: One item in this many is deliberately defective. Set to 0 to disable.
        self.defect_rate = defect_rate

    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int | None = None,
        cached_context: str | None = None,
    ) -> ProviderResponse:
        seed = int(
            hashlib.sha256(f"{system}\n{prompt}".encode()).hexdigest()[:12], 16
        )
        rng = random.Random(seed)

        root = schema.get("properties", {})
        if "questions" in root:
            data: Any = {"questions": self._questions(prompt, rng)}
        elif "sections" in root:
            data = self._article(prompt, rng)
        else:
            data = {}

        approximate_input = (len(system) + len(prompt) + len(cached_context or "")) // 4
        approximate_output = len(json.dumps(data)) // 4
        return ProviderResponse(
            data=data,
            usage=Usage(
                input_tokens=approximate_input,
                output_tokens=approximate_output,
            ),
            model=self.model,
            provider=self.name,
            notes=[
                "Generated by the offline provider. This content is synthetic "
                "and is not clinically valid."
            ],
        )

    # ---- item generation -------------------------------------------------
    def _questions(self, prompt: str, rng: random.Random) -> list[dict[str, Any]]:
        count = self._requested_count(prompt)
        topics = self._requested_topics(prompt) or ["general"]
        out: list[dict[str, Any]] = []

        for index in range(count):
            topic = topics[index % len(topics)]
            presentation = rng.choice(_MOCK_PRESENTATIONS)
            actions = rng.sample(_MOCK_ACTIONS, 5)
            correct_index = rng.randrange(5)
            keys = ["A", "B", "C", "D", "E"]

            options = [
                {
                    "key": keys[i],
                    "text": actions[i],
                    "is_correct": i == correct_index,
                    "rationale": (
                        "This is the single best answer for the presentation "
                        "described."
                        if i == correct_index
                        else "Reasonable but not the best next step here; it "
                        "delays the definitive action."
                    ),
                }
                for i in range(5)
            ]

            item: dict[str, Any] = {
                # Composed from several varying parts rather than a fixed
                # template, so successive items are genuinely different. A
                # boilerplate stem would be flagged as a near-duplicate of the
                # previous one — correctly — and the pipeline's tests would end
                # up measuring the test double instead of the pipeline.
                "stem": (
                    f"[SYNTHETIC — OFFLINE PROVIDER] You are asked to review "
                    f"{presentation}. {rng.choice(_MOCK_FINDINGS)}. "
                    f"{rng.choice(_MOCK_CONTEXTS)}. The working diagnosis falls "
                    f"under {topic}."
                ),
                "lead_in": "What is the single most appropriate next step?",
                "options": options,
                "explanation": (
                    "This item was produced by the offline provider for pipeline "
                    "testing. It carries no clinical authority and must not be "
                    "published. The correct option is the one that addresses the "
                    "immediate threat to life or limb before further imaging."
                ),
                "references": rng.sample(_MOCK_REFERENCES, 2),
                "topic": topic,
                "subtopic": f"{topic} — assessment",
                "blueprint_category": self._category_for(index),
                "difficulty_band": ["easy", "moderate", "advanced", "consultant",
                                    "fellowship"][index % 5],
                "bloom_level": ["apply", "analyse", "evaluate"][index % 3],
                "competency_domain": "medical_knowledge",
                "learning_objectives": [
                    f"Recognise the presenting features relevant to {topic}.",
                    f"Select an appropriate initial management step in {topic}.",
                ],
                "ai_confidence": round(0.55 + rng.random() * 0.4, 3),
            }

            if self.defect_rate and index % self.defect_rate == self.defect_rate - 1:
                item = self._introduce_defect(item, index // self.defect_rate)
            out.append(item)
        return out

    def _introduce_defect(self, item: dict[str, Any], which: int) -> dict[str, Any]:
        """Break an item in one specific way, cycling through the failure modes.

        Each corresponds to a check in ``app.services.ai.quality``. If a check
        is ever removed or broken, the pipeline test that counts rejections
        starts failing — which is the point.
        """
        defects = [
            # Two keys: violates single-best-answer.
            lambda i: {
                **i,
                "options": [
                    {**o, "is_correct": idx in (0, 1)}
                    for idx, o in enumerate(i["options"])
                ],
            },
            # An absolute term in an option, which reliably signals the answer.
            lambda i: {
                **i,
                "options": [
                    {**o, "text": "Always " + o["text"].lower()} if idx == 0 else o
                    for idx, o in enumerate(i["options"])
                ],
            },
            # "All of the above", which tests reading rather than knowledge.
            lambda i: {
                **i,
                "options": [
                    {**o, "text": "All of the above"} if idx == 4 else o
                    for idx, o in enumerate(i["options"])
                ],
            },
            # No explanation, so a candidate learns nothing from review.
            lambda i: {**i, "explanation": ""},
            # No references, so nothing can be checked.
            lambda i: {**i, "references": []},
            # Four options where the house style is five.
            lambda i: {**i, "options": i["options"][:4]},
            # A patient identifier, which must never enter the bank.
            lambda i: {
                **i,
                "stem": i["stem"] + " Hospital number MRN-4471903, Mrs A Okafor.",
            },
        ]
        return defects[which % len(defects)](item)

    # ---- article generation ---------------------------------------------
    def _article(self, prompt: str, rng: random.Random) -> dict[str, Any]:
        from app.services.ai.cme_author import ARTICLE_SECTIONS

        topic = (self._requested_topics(prompt) or ["the specified topic"])[0]
        sections = [
            {
                "key": key,
                "title": title,
                "order": order,
                "body": (
                    f"[SYNTHETIC — OFFLINE PROVIDER] This section would cover "
                    f"{title.lower()} as it relates to {topic}. It is placeholder "
                    "text generated without a model and carries no clinical "
                    "authority."
                ),
            }
            for order, (key, title) in enumerate(ARTICLE_SECTIONS)
        ]
        return {
            "title": f"[SYNTHETIC] {topic.title()}: a structured review",
            "sections": sections,
            "learning_objectives": [
                f"Describe the basic science underpinning {topic}.",
                f"Outline the assessment of a patient presenting with {topic}.",
                f"Summarise current guideline recommendations for {topic}.",
            ],
            "clinical_pearls": [
                "This pearl is synthetic and must not be relied upon.",
            ],
            "frequently_tested_areas": [f"{topic} — initial management"],
            "landmark_trials": [],
            "references": [
                {
                    "n": i + 1,
                    "vancouver": reference,
                    "apa": reference,
                    "doi": None,
                    "source": "textbook",
                }
                for i, reference in enumerate(rng.sample(_MOCK_REFERENCES, 3))
            ],
            "estimated_minutes": 20,
        }

    # ---- prompt parsing --------------------------------------------------
    @staticmethod
    def _requested_count(prompt: str) -> int:
        match = re.search(r"(?:write|generate|produce)\s+(\d+)", prompt, re.I)
        if match:
            return max(1, min(50, int(match.group(1))))
        return 5

    @staticmethod
    def _requested_topics(prompt: str) -> list[str]:
        match = re.search(r"Topics:\s*(.+)", prompt)
        if not match:
            return []
        return [t.strip() for t in match.group(1).split(",") if t.strip()][:12]

    @staticmethod
    def _category_for(index: int) -> str:
        from app.services.cbt_engine import DEFAULT_BLUEPRINT

        categories = list(DEFAULT_BLUEPRINT)
        return categories[index % len(categories)]


# ==========================================================================
# Selection
# ==========================================================================
def get_provider(*, force_mock: bool = False) -> AiProvider:
    """The provider this deployment should use.

    Falls back to the offline provider rather than raising whenever generation
    is switched off, so every calling path works out of the box and a
    department can watch the whole pipeline run before deciding whether to
    spend anything on it.
    """
    if force_mock or not settings.enable_ai_generation:
        return MockProvider()
    return AnthropicProvider()


def describe_provider(provider: AiProvider) -> dict[str, Any]:
    """What the API reports about which engine produced content."""
    is_mock = provider.name == MockProvider.name
    return {
        "provider": provider.name,
        "model": provider.model,
        "is_offline_placeholder": is_mock,
        "warning": (
            "Content is generated by the offline placeholder provider. It is "
            "structurally valid and clinically meaningless. Set "
            "RTC_ENABLE_AI_GENERATION=true and RTC_AI_API_KEY to generate real "
            "content."
            if is_mock
            else None
        ),
    }
