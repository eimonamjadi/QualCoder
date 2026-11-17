from pathlib import Path

from qualcode.retrieval import BM25Retriever, Item, tokenize
from qualcode.segment import segment

SAMPLE = (Path(__file__).resolve().parent.parent / "samples" / "interview_transcript.txt").read_text()


def test_segment_offsets_point_into_original_text():
    passages = segment(SAMPLE)
    assert len(passages) >= 10
    for p in passages:
        assert SAMPLE[p.start:p.end].strip() == p.text


def test_speaker_turns_split_even_without_blank_lines():
    text = "Interviewer: How was it?\nP01: Fine.\nIt was fine.\nInterviewer: Ok."
    parts = [p.text for p in segment(text)]
    assert parts == ["Interviewer: How was it?", "P01: Fine.\nIt was fine.", "Interviewer: Ok."]


def test_long_paragraph_is_split_on_sentences():
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    parts = segment(text, max_chars=300)
    assert len(parts) > 5
    assert all(len(p.text) <= 300 for p in parts)
    assert all(text[p.start:p.end].strip() == p.text for p in parts)


def test_bm25_ranks_the_relevant_item_first():
    items = [
        Item("EX:1", "example", "I worked thirty hours a week to pay rent"),
        Item("EX:2", "example", "The study group became my people"),
        Item("EX:3", "example", "I asked ChatGPT to explain the first step"),
    ]
    top = BM25Retriever(items).top("my study group felt like my people", k=1)
    assert top[0].ref == "EX:2"


def test_tokenize_drops_stopwords_and_stems():
    assert tokenize("The feelings of belonging") == ["feeling", "belong"]
