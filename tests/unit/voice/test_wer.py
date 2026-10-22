from caregap.voice.wer import align, name_hits, tokens, wer


def test_tokens_normalize_case_punctuation_and_apostrophes() -> None:
    curly = "\N{RIGHT SINGLE QUOTATION MARK}"
    assert tokens(f"Hello, Ava! Don{curly}t forget: press 9 - or say STOP.") == [
        "hello", "ava", "don't", "forget", "press", "9", "or", "say", "stop",
    ]  # fmt: skip


def test_wer_counts_each_error_type() -> None:
    r = wer("take your lisinopril every day", "take the lice in a pril every day today")
    # your->the (S), lisinopril->lice (S), +in +a +pril (I x3), +today (I)
    assert (r.ref_words, r.substitutions, r.deletions, r.insertions) == (5, 2, 0, 4)
    assert r.wer == 6 / 5


def test_perfect_and_empty() -> None:
    assert wer("Hello there.", "hello there").wer == 0.0
    assert wer("hello there", "").deletions == 2
    assert wer("", "noise").wer == 0.0


def test_alignment_is_minimal() -> None:
    ops = align(["a", "b", "c"], ["a", "c"])
    assert [o.kind for o in ops] == ["C", "D", "C"]


def test_name_hits_attributes_errors_to_name_spans() -> None:
    ref = "Sunrise Medicare Advantage covers atorvastatin and atorvastatin refills"
    hyp = "Sunrise Medicare Advantage covers a tour of a statin and atorvastatin refills"
    hits = name_hits(ref, hyp, ["Sunrise Medicare Advantage", "atorvastatin"])
    assert [(h.name, h.correct) for h in hits] == [
        ("Sunrise Medicare Advantage", True),
        ("atorvastatin", False),
        ("atorvastatin", True),
    ]
    assert hits[1].word_errors == 1


def test_multi_word_name_partly_wrong() -> None:
    (hit,) = name_hits("Horizon ACA Marketplace plan", "horizon aka marketplace plan",
                       ["Horizon ACA Marketplace"])  # fmt: skip
    assert (hit.correct, hit.word_errors, hit.heard) == (False, 1, "horizon aka marketplace")
