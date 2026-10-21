import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.wechat_url import normalize_wechat_short_url, short_id_from_review_id


class WeChatShortUrlTests(unittest.TestCase):
    def test_restores_tildes_in_short_link_path(self):
        self.assertEqual(
            normalize_wechat_short_url(
                "https://mp.weixin.qq.com/s/oi~Lh3K0LZ92f~LtQBSljQ"
            ),
            "https://mp.weixin.qq.com/s/oi_Lh3K0LZ92f_LtQBSljQ",
        )

    def test_preserves_query_and_fragment(self):
        self.assertEqual(
            normalize_wechat_short_url(
                "https://mp.weixin.qq.com/s/oi~Lh3K0LZ92f~LtQBSljQ?from=share#wechat_redirect"
            ),
            "https://mp.weixin.qq.com/s/oi_Lh3K0LZ92f_LtQBSljQ?from=share#wechat_redirect",
        )

    def test_does_not_change_tildes_outside_the_short_link_path(self):
        url = "https://example.com/s/oi~Lh3K0LZ92f~LtQBSljQ"
        self.assertEqual(normalize_wechat_short_url(url), url)
        url = "https://mp.weixin.qq.com/s/oi_Lh3K0LZ92f_LtQBSljQ?key=~value"
        self.assertEqual(normalize_wechat_short_url(url), url)

    def test_does_not_change_malformed_or_non_short_paths(self):
        for url in (
            "https://mp.weixin.qq.com/s/short~id",
            "https://mp.weixin.qq.com/s/oi~Lh3K0LZ92f~LtQBSljQ/extra",
            "https://mp.weixin.qq.com.evil.example/s/oi~Lh3K0LZ92f~LtQBSljQ",
        ):
            self.assertEqual(normalize_wechat_short_url(url), url)

    def test_extracts_review_id_without_losing_short_id_underscores(self):
        review_id = "MP_WXS_3293703638_oi_Lh3K0LZ92f_LtQBSljQ"
        self.assertEqual(
            short_id_from_review_id(review_id),
            "oi_Lh3K0LZ92f_LtQBSljQ",
        )

    def test_restores_tildes_in_review_id_short_id(self):
        review_id = "MP_WXS_3293703638_oi~Lh3K0LZ92f~LtQBSljQ"
        self.assertEqual(
            short_id_from_review_id(review_id),
            "oi_Lh3K0LZ92f_LtQBSljQ",
        )

    def test_rejects_malformed_review_ids(self):
        for review_id in (
            "",
            "MP_WXS_no-account_oi_Lh3K0LZ92f_LtQBSljQ",
            "unrelated_prefix_oi_Lh3K0LZ92f_LtQBSljQ",
            "MP_WXS_3293703638_short",
        ):
            self.assertEqual(short_id_from_review_id(review_id), "")


if __name__ == "__main__":
    unittest.main()
