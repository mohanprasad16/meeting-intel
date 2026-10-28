from app.chunking import chunk_turns
from app.parsing import Turn


def turns(n, words=5):
    return [Turn(i, i * 10, f"S{i % 3}", " ".join(["word"] * words) + ".") for i in range(1, n + 1)]


def test_windows_overlap_by_one_turn():
    chunks = chunk_turns(turns(12), max_turns=5)
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 5), (5, 9), (9, 12)]


def test_word_limit_closes_window_early():
    chunks = chunk_turns(turns(6, words=100), max_turns=5, max_words=300)
    assert chunks[0].end_line == 3


def test_long_monologue_split_at_sentences():
    long = Turn(1, 0, "Ravi", " ".join(f"Sentence number {i} is here." for i in range(100)))
    chunks = chunk_turns([long], max_words=50)
    assert len(chunks) > 1
    assert all(c.text.startswith("Ravi: Sentence") for c in chunks)
    assert all(c.start_line == 1 for c in chunks)


def test_chunk_carries_speakers_times_and_text():
    c = chunk_turns(turns(3))[0]
    assert c.speakers == ["S1", "S2", "S0"]
    assert (c.start_time, c.end_time) == (10, 30)
    assert c.text.splitlines()[0] == "S1: word word word word word."


def test_single_turn_and_empty():
    assert len(chunk_turns(turns(1))) == 1
    assert chunk_turns([]) == []
