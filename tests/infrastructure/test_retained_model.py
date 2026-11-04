import pytest
import retained_model


def test_paired_profiles_change_only_ngram_settings():
    uid = "11111111-2222-3333-4444-555555555555"
    baseline = retained_model.values("baseline", uid)
    ngram = retained_model.values("ngram", uid)
    assert baseline["engine"]["speculativeDecoding"] is None
    assert ngram["engine"]["speculativeDecoding"] == {
        "method": "ngram",
        "num_speculative_tokens": 5,
        "prompt_lookup_min": 2,
        "prompt_lookup_max": 5,
    }
    for value in (baseline, ngram):
        assert value["model"]["repository"] == "Qwen/Qwen3-4B"
        assert value["nodeUID"] == uid and value["gpus"] == 1
        value.pop("profileRef")
        value.pop("profileSHA256")
        value["engine"].pop("speculativeDecoding")
    assert baseline == ngram
    with pytest.raises(ValueError):
        retained_model.values("other-profile", uid)
