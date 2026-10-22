import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from txtstat import summarize  # noqa: E402


class TestSummarize(unittest.TestCase):
    def test_counts_words_and_sentences(self):
        text = "Hello world. This is fine! Is it?"
        result = summarize(text)
        self.assertEqual(result["words"], 7)
        self.assertEqual(result["sentences"], 3)

    def test_unique_is_case_and_punctuation_insensitive(self):
        result = summarize("Hello, hello! HELLO.")
        self.assertEqual(result["unique"], 1)

    def test_average_word_length_ignores_punctuation(self):
        result = summarize("Hi, ok.")
        self.assertEqual(result["avg_word_length"], 2.0)

    def test_words_counts_duplicates(self):
        result = summarize("one two two three")
        self.assertEqual(result["words"], 4)
        self.assertEqual(result["unique"], 3)

    def test_empty_text(self):
        self.assertEqual(
            summarize(""),
            {"words": 0, "sentences": 0, "unique": 0, "avg_word_length": 0.0},
        )

    def test_hyphenated_words_stay_one_word(self):
        result = summarize("A well-known state-of-the-art tool.")
        self.assertEqual(result["words"], 4)
        self.assertEqual(result["unique"], 4)


if __name__ == "__main__":
    unittest.main()
