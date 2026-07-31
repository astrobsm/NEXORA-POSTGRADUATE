"""Classical test theory, checked against hand-computable cases.

Every assertion here is a number that can be worked out on paper. That matters
more than usual: a reliability coefficient that is subtly wrong looks exactly
like one that is right, and no downstream test would catch it.
"""

from __future__ import annotations

import math

import pytest

from app.services import psychometrics as p


class TestVariance:
    def test_population_not_sample(self) -> None:
        """Population variance, deliberately.

        For [1, 2, 3, 4, 5] the population variance is 2.0 and the sample
        variance 2.5. KR-20 and alpha are defined over the observed group, so
        the population form is correct — and getting it wrong would inflate
        both coefficients by an amount small enough to look plausible.
        """
        assert p._variance([1, 2, 3, 4, 5]) == pytest.approx(2.0)

    def test_empty_is_zero_not_an_error(self) -> None:
        assert p._variance([]) == 0.0


class TestFacility:
    def test_proportion_correct(self) -> None:
        assert p.facility_index([1, 1, 1, 0]) == pytest.approx(0.75)

    def test_partial_credit_scales_by_max_mark(self) -> None:
        # Two candidates scored 1 of 2 marks; facility is 0.5, not 1.0.
        assert p.facility_index([1.0, 1.0], max_mark=2.0) == pytest.approx(0.5)

    def test_no_candidates_is_undefined(self) -> None:
        assert p.facility_index([]) is None


class TestKr20:
    #: Six candidates, five items. Worked by hand in the docstring below.
    MATRIX = [
        [1.0, 1.0, 1.0, 1.0, 0.0],
        [1.0, 1.0, 1.0, 0.0, 0.0],
        [1.0, 1.0, 0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0, 0.0, 0.0],
        [1.0, 1.0, 1.0, 1.0, 1.0],
        [0.0, 0.0, 0.0, 0.0, 0.0],
    ]

    def test_matches_hand_computation(self) -> None:
        """KR-20 = (k/(k-1)) * (1 - sum(pq) / var(total)).

        Item p values are 5/6, 4/6, 3/6, 2/6, 1/6, so sum(pq) = 0.9722.
        Totals are 4, 3, 2, 1, 5, 0 with mean 2.5 and population variance
        17.5/6 = 2.9167. KR-20 = 1.25 * (1 - 0.9722/2.9167) = 0.8333.
        """
        assert p.kr20(self.MATRIX) == pytest.approx(0.8333, abs=1e-4)

    def test_equals_alpha_for_dichotomous_items(self) -> None:
        """The two are algebraically identical when every item is 0 or 1.

        Both are stored because an examinations board that asked for KR-20 will
        not accept "they are the same here" without seeing both — so they had
        better actually agree.
        """
        assert p.kr20(self.MATRIX) == pytest.approx(p.cronbach_alpha(self.MATRIX))

    def test_zero_variance_is_undefined_not_zero(self) -> None:
        """Everyone scoring identically has no reliability to measure.

        Returning 0.0 would read as "unreliable"; the truth is "unmeasurable",
        and an examinations officer must be able to tell those apart.
        """
        assert p.kr20([[1.0, 1.0], [1.0, 1.0]]) is None

    def test_single_candidate_is_undefined(self) -> None:
        assert p.kr20([[1.0, 0.0, 1.0]]) is None

    def test_single_item_is_undefined(self) -> None:
        # k - 1 would be zero: the formula does not exist for one item.
        assert p.kr20([[1.0], [0.0]]) is None


class TestPointBiserial:
    def test_discriminating_item_is_strongly_positive(self) -> None:
        """An item only the top half answered correctly.

        Note it does *not* reach 1.0, and should not. Against the rest score —
        totals with this item's own mark removed — the two groups are
        [3, 2] and [2, 1], which overlap at 2. The exact value is 1/sqrt(2).
        An implementation that returned 1.0 here would be correlating the item
        with a total that already contains it.
        """
        item = [1.0, 1.0, 0.0, 0.0]
        totals = [4.0, 3.0, 2.0, 1.0]
        assert p.point_biserial(item, totals) == pytest.approx(
            1 / math.sqrt(2), abs=1e-9
        )

    def test_uncorrected_formula_would_overstate_it(self) -> None:
        """Guards the correction itself rather than a value it happens to give.

        Correlating the item against the raw total (rather than the rest) is
        the mistake this module exists to avoid; that value is strictly larger.
        """
        item = [1.0, 1.0, 0.0, 0.0]
        totals = [4.0, 3.0, 2.0, 1.0]
        corrected = p.point_biserial(item, totals)

        n = len(item)
        item_mean, total_mean = sum(item) / n, sum(totals) / n
        cov = sum((i - item_mean) * (t - total_mean) for i, t in zip(item, totals, strict=True)) / n
        uncorrected = cov / (
            math.sqrt(p._variance(item)) * math.sqrt(p._variance(totals))
        )
        assert corrected < uncorrected

    def test_negatively_discriminating_item_is_negative(self) -> None:
        """The weaker candidates did better — almost always a miskeyed item.

        This is the single most valuable finding the module produces, so it
        gets its own test.
        """
        item = [0.0, 0.0, 1.0, 1.0]
        totals = [4.0, 3.0, 2.0, 1.0]
        assert p.point_biserial(item, totals) < 0

    def test_corrected_against_rest_score(self) -> None:
        """The correlation excludes the item's own contribution to the total.

        Without the correction every item correlates with itself through the
        total. On a two-item paper the uncorrected value would be near 1.0 for
        any item; corrected, an item uncorrelated with the rest gives None or a
        small value rather than a flattering one.
        """
        item = [1.0, 0.0, 1.0, 0.0]
        # The rest score is constant, so the correlation is undefined rather
        # than the spuriously high value an uncorrected formula would produce.
        totals = [2.0, 1.0, 2.0, 1.0]
        assert p.point_biserial(item, totals) is None

    def test_constant_item_is_undefined(self) -> None:
        assert p.point_biserial([1.0, 1.0, 1.0], [3.0, 2.0, 1.0]) is None


class TestDiscriminationIndex:
    def test_upper_minus_lower(self) -> None:
        item = [1.0, 1.0, 1.0, 0.0, 0.0, 0.0]
        totals = [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]
        # 27% of 6 rounds to 2: top two both correct, bottom two both wrong.
        assert p.discrimination_index(item, totals) == pytest.approx(1.0)

    def test_too_few_candidates_is_undefined(self) -> None:
        assert p.discrimination_index([1.0], [1.0]) is None


class TestStandardError:
    def test_perfect_reliability_gives_zero_error(self) -> None:
        matrix = [[1.0, 1.0], [0.0, 0.0]]
        assert p.standard_error_of_measurement(matrix, 1.0) == pytest.approx(0.0)

    def test_scales_with_spread(self) -> None:
        matrix = [[1.0, 1.0, 1.0], [0.0, 0.0, 0.0], [1.0, 0.0, 1.0]]
        sem = p.standard_error_of_measurement(matrix, 0.5)
        sd = math.sqrt(p._variance([sum(r) for r in matrix]))
        assert sem == pytest.approx(sd * math.sqrt(0.5))

    def test_undefined_reliability_gives_undefined_error(self) -> None:
        assert p.standard_error_of_measurement([[1.0], [0.0]], None) is None


class TestDistractorAnalysis:
    def test_flags_a_distractor_nobody_chose(self) -> None:
        selections = [["A"], ["A"], ["B"], ["A"]]
        stats = p.distractor_analysis(
            selections, [4.0, 3.0, 2.0, 1.0], ["A", "B", "C"], ["A"]
        )
        assert "non_functioning" in stats["C"]["flags"]
        assert stats["C"]["count"] == 0

    def test_flags_a_distractor_that_attracts_strong_candidates(self) -> None:
        """A distractor the best candidates prefer usually means a wrong key.

        Worth catching: the item looks merely difficult, and the statistics are
        the only thing that says otherwise.
        """
        selections = [["B"], ["B"], ["A"], ["A"]]
        stats = p.distractor_analysis(
            selections, [4.0, 3.0, 2.0, 1.0], ["A", "B"], ["A"]
        )
        assert "attracts_strong_candidates" in stats["B"]["flags"]

    def test_the_key_is_marked_as_such(self) -> None:
        stats = p.distractor_analysis([["A"]], [1.0], ["A", "B"], ["A"])
        assert stats["A"]["is_key"] is True
        assert stats["B"]["is_key"] is False

    def test_omissions_do_not_inflate_shares(self) -> None:
        """A candidate who answered nothing still counts in the denominator.

        Shares therefore do not sum to 1 on a paper with omissions. That is
        intended: an item nobody attempted is itself a finding.
        """
        selections = [["A"], [], [], []]
        stats = p.distractor_analysis(selections, [1.0, 1.0, 1.0, 1.0], ["A", "B"], ["A"])
        assert stats["A"]["share"] == pytest.approx(0.25)
        assert sum(s["share"] for s in stats.values()) < 1.0


class TestDifficultyBands:
    @pytest.mark.parametrize(
        ("facility", "expected"),
        [
            (0.95, "easy"),
            (0.80, "easy"),
            (0.79, "moderate"),
            (0.60, "moderate"),
            (0.45, "advanced"),
            (0.30, "consultant"),
            (0.10, "fellowship"),
            (0.00, "fellowship"),
        ],
    )
    def test_boundaries(self, facility: float, expected: str) -> None:
        assert p.band_for_facility(facility) == expected

    def test_perfect_facility_is_still_a_band(self) -> None:
        """1.0 must not fall through the ranges.

        The easy band's upper bound is 1.01 precisely so an item everyone
        answered correctly still lands somewhere.
        """
        assert p.band_for_facility(1.0) == "easy"

    def test_band_to_difficulty_inverts_facility(self) -> None:
        # Fellowship standard spans facility 0.00-0.20, midpoint 0.10, so
        # difficulty (1 - facility) is 0.90.
        assert p.band_to_difficulty("fellowship") == pytest.approx(0.90)
        assert p.band_to_difficulty("easy") == pytest.approx(0.10)

    def test_difficulty_rises_monotonically_across_the_bands(self) -> None:
        """Harder bands must map to strictly higher difficulty.

        The assembler filters on the numeric difficulty, so an inversion here
        would quietly serve fellowship-standard items to house officers.
        """
        ordered = ["easy", "moderate", "advanced", "consultant", "fellowship"]
        values = [p.band_to_difficulty(b) for b in ordered]
        assert values == sorted(values)
        assert len(set(values)) == len(values)


class TestBlueprintCoverage:
    def test_reports_requested_against_delivered(self) -> None:
        class FakeQuestion:
            def __init__(self, category: str | None) -> None:
                self.blueprint_category = category

        questions = [FakeQuestion("basic_sciences")] * 2 + [FakeQuestion("clinical_medicine")] * 8
        coverage = p.blueprint_coverage(
            questions, {"basic_sciences": 0.10, "clinical_medicine": 0.90}
        )
        assert coverage["total_items"] == 10
        assert coverage["categories"]["basic_sciences"]["delivered_share"] == pytest.approx(0.2)
        assert coverage["categories"]["basic_sciences"]["variance"] == pytest.approx(0.1)

    def test_unclassified_items_are_reported_not_hidden(self) -> None:
        class FakeQuestion:
            blueprint_category = None

        coverage = p.blueprint_coverage([FakeQuestion()], {})
        assert coverage["unclassified_count"] == 1
