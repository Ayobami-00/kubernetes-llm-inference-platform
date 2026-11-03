import pytest
from workload_runner.reporting.quality import (
    answer_scores,
    corpus_gleu,
    gleu_stats,
    supporting_scores,
)


def test_gleu_reference_statistics():
    # Fixed statistics from cnap/gec-ranking scripts/gleu.py, updated 2017 scorer.
    assert gleu_stats("a b c d e", "a b x d e", "a b x d e") == [5, 5, 5, 5, 4, 4, 3, 3, 2, 2]
    assert gleu_stats("a b c d e", "a b c d e", "a b x d e") == [5, 5, 3, 5, 0, 4, 0, 3, 0, 2]
    assert corpus_gleu([("a b c d e", "a b x d e", ["a b x d e"])]) == 1.0
    assert corpus_gleu([("a b c d e", "a b c d e", ["a b x d e"])]) == 0.0
    assert corpus_gleu([]) is None


def test_gleu_deterministic_multiple_references():
    examples = [("a b c d e", "a b x d e", ["a b x d e", "a b y d e"])]
    assert corpus_gleu(examples) == corpus_gleu(examples)
    assert 0 < corpus_gleu(examples) < 1


@pytest.mark.parametrize(
    ("prediction", "reference", "expected"),
    [
        ("The Paris.", "Paris", (1, 1)),
        ("yes", "no", (0, 0)),
        ("red green", "green blue", (0, 0.5)),
        ("", "", (1, 1)),
    ],
)
def test_answer_reference_cases(prediction, reference, expected):
    assert answer_scores(prediction, reference) == expected


def test_supporting_facts():
    assert supporting_scores([["France", 0]], [["France", 0]]) == (1, 1)
    assert supporting_scores([["France", 1]], [["France", 0]]) == (0, 0)
