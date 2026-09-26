"""冒烟测试：守护 get-m3u 的模块导入图。

历史教训：CI 曾两次死于模块导入（requirements 缺 requests、utils.py vs utils/ 包冲突）。
get-m3u 此前没有任何测试，本文件提供最小回归网。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TestModuleImports(unittest.TestCase):
    def test_main_imports(self):
        import main as M
        for attr in ["scrape_fofa", "filter_segments", "run_native_scan",
                     "select_scan_segments", "update_scan_state",
                     "update_discovery_database", "load_external_feedback"]:
            self.assertTrue(hasattr(M, attr), f"main 缺少公开符号: {attr}")

    def test_utils_package_imports(self):
        """utils/ 必须是包（含 __init__），crawler/fofa_cookie 子模块可导入"""
        import utils
        import utils.crawler
        import utils.fofa_cookie
        for attr in ["live_print", "atomic_write", "parse_rtp_entries", "build_m3u"]:
            self.assertTrue(hasattr(utils, attr), f"utils 缺少: {attr}")
        self.assertTrue(hasattr(utils.crawler, "crawl_segments"))
        self.assertTrue(hasattr(utils.fofa_cookie, "check_cookie_workflow"))

    def test_probe_imports(self):
        import probe
        for attr in ["load_source_scores", "update_source_score", "filter_by_score"]:
            self.assertTrue(hasattr(probe, attr), f"probe 缺少: {attr}")

    def test_scan_quota_rotation(self):
        """配额轮换：生产中段必扫，dead_ips 段降权"""
        os.environ["SCAN_SEGMENT_QUOTA"] = "5"
        import main as M
        M.SCAN_SEGMENT_QUOTA = 5
        valid = [f"10.0.{i}" for i in range(10)]
        sel, _ = M.select_scan_segments(valid, ["10.0.1.5:4000"],
                                        dead_ips=["10.0.5.9:4000"])
        self.assertEqual(len(sel), 5)
        self.assertIn("10.0.1", sel)
        self.assertNotIn("10.0.5", sel)
        if os.path.exists(M.SCAN_STATE_FILE):
            os.remove(M.SCAN_STATE_FILE)


if __name__ == "__main__":
    unittest.main()
