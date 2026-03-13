import unittest

from scripts.sync_google_reviews import extract_review_text


class ExtractReviewTextTests(unittest.TestCase):
    def test_extracts_review_before_stars(self):
        stars = "\u2605" * 5
        description = f"Trevligt bemotande i butiken Power i Skovde. {stars}POWER Skovde  (7155)"
        self.assertEqual(
            extract_review_text(description),
            "Trevligt bemotande i butiken Power i Skovde.",
        )

    def test_returns_none_when_no_review_text(self):
        stars = ("\u2605" * 4) + "\u2606"
        description = f" {stars}POWER Mariero  (1690)Langflatveien 21 Stavanger NO51 52 02 00"
        self.assertIsNone(extract_review_text(description))

    def test_handles_long_multiline_review(self):
        stars = "\u2605" + ("\u2606" * 4)
        description = (
            f"Line one.\nLine two.\n\n(Original)\nRad ett.\nRad tva! {stars}POWER Malmo  (7140)"
        )
        self.assertEqual(
            extract_review_text(description),
            "Line one.\nLine two.\n\n(Original)\nRad ett.\nRad tva!",
        )


if __name__ == "__main__":
    unittest.main()
