#!/usr/bin/env python3
"""Smoke tests for the hospital-procurement-monitor core helpers.

Runs with the standard-library unittest (no third-party dependencies):

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import monitor_common as mc
from collect_notices import date_to_iso, is_structural_noise, text_date
from prepare_candidates import classify, term_match

PUBLIC_SOCKADDR = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]


class UrlValidationTests(unittest.TestCase):
    """validate_public_http_url must reject anything that is not a public web URL."""

    @mock.patch("socket.getaddrinfo", return_value=PUBLIC_SOCKADDR)
    def test_accepts_public_http(self, _getaddrinfo) -> None:
        url = mc.validate_public_http_url("https://www.example.gov.cn/notices")
        self.assertTrue(url.startswith("https://www.example.gov.cn/notices"))

    def test_rejects_non_http_scheme(self) -> None:
        for bad in ("ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    mc.validate_public_http_url(bad)

    @mock.patch("socket.getaddrinfo", return_value=PUBLIC_SOCKADDR)
    def test_rejects_localhost(self, _getaddrinfo) -> None:
        with self.assertRaises(ValueError):
            mc.validate_public_http_url("http://localhost:8080/notices")

    @mock.patch("socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    def test_rejects_loopback_resolution(self, _getaddrinfo) -> None:
        with self.assertRaises(ValueError):
            mc.validate_public_http_url("http://intranet.example/x")

    def test_rejects_private_ip(self) -> None:
        for bad in ("http://127.0.0.1/x", "http://10.0.0.5/x", "http://192.168.1.1/x",
                    "http://[::1]/x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    mc.validate_public_http_url(bad)


class CanonicalUrlTests(unittest.TestCase):
    """canonical_url normalizes scheme/host; query is preserved (may distinguish
    announcements), fragment is dropped, path case and trailing slash kept as-is.
    This matches the de-dup contract used across the pipeline."""

    def test_normalizes_host_case_and_scheme(self) -> None:
        self.assertEqual(
            mc.canonical_url("HTTPS://Example.COM/A?x=1#frag"),
            "https://example.com/A?x=1",
        )

    def test_drops_fragment_keeps_query(self) -> None:
        self.assertEqual(mc.canonical_url("https://a.cn/p?q=1#top"), "https://a.cn/p?q=1")

    def test_keeps_trailing_slash(self) -> None:
        self.assertEqual(mc.canonical_url("https://a.cn/p/"), "https://a.cn/p/")


class NoticeIdTests(unittest.TestCase):
    """make_notice_id is deterministic and stable for identical inputs."""

    def test_deterministic(self) -> None:
        one = mc.make_notice_id("s1", "https://a.cn/n1", "公告标题")
        two = mc.make_notice_id("s1", "https://a.cn/n1", "公告标题")
        self.assertEqual(one, two)
        self.assertEqual(len(one), 64)  # sha256 hex

    def test_differs_on_url_or_title(self) -> None:
        base = mc.make_notice_id("s1", "https://a.cn/n1", "标题A")
        other_url = mc.make_notice_id("s1", "https://a.cn/n2", "标题A")
        other_title = mc.make_notice_id("s1", "https://a.cn/n1", "标题B")
        self.assertNotEqual(base, other_url)
        self.assertNotEqual(base, other_title)


class DateParsingTests(unittest.TestCase):
    """Chinese and mixed-format dates parse to ISO."""

    def test_standard_chinese_date(self) -> None:
        self.assertEqual(text_date("2026年8月5日公告"), "2026年8月5日")
        self.assertEqual(date_to_iso("2026年8月5日"), "2026-08-05")

    def test_slashed_and_dashed_dates(self) -> None:
        self.assertEqual(date_to_iso("2026-08-05"), "2026-08-05")
        self.assertEqual(date_to_iso("2026/08/05"), "2026-08-05")
        self.assertEqual(date_to_iso("2026.8.5"), "2026-08-05")

    def test_mmddyyyy_without_separator(self) -> None:
        self.assertEqual(date_to_iso("05-292026"), "2026-05-29")

    def test_invalid_date_returns_empty(self) -> None:
        self.assertEqual(date_to_iso("2026-13-40"), "")

    def test_no_date_returns_empty(self) -> None:
        self.assertEqual(text_date("无日期标题"), "")
        self.assertEqual(date_to_iso(""), "")


class KeywordMatchingTests(unittest.TestCase):
    """ASCII keywords match on word boundaries; CJK on substring.
    Note: term_match expects lowercased text (pipeline lowercases before calling)."""

    def test_ascii_word_boundary(self) -> None:
        self.assertTrue(term_match("CT", "ct设备招标"))
        self.assertFalse(term_match("CT", "doctor appointment"))  # inside "doctor"
        self.assertFalse(term_match("CT", "historical"))  # inside "historical"

    def test_cjk_substring(self) -> None:
        self.assertTrue(term_match("互联网医院", "互联网医院建设项目"))
        self.assertFalse(term_match("互联网医院", "医院宽带改造"))

    def test_classify_required_and_exclude(self) -> None:
        rules = {"required": ["互联网医院"], "synonyms": [], "exclude": ["宽带"]}
        gate, evidence = classify({"title": "互联网医院建设项目招标"}, rules)
        self.assertEqual(gate, "candidate")
        self.assertIn("互联网医院", evidence["required_keywords"])

        gate, evidence = classify({"title": "互联网医院互联网宽带升级"}, rules)
        self.assertEqual(gate, "excluded")
        self.assertIn("宽带", evidence["exclude_keywords"])

        gate, _ = classify({"title": "食堂食材采购"}, rules)
        self.assertEqual(gate, "irrelevant")


class NoiseFilterTests(unittest.TestCase):
    """Short titles pointing at list/column URLs are structural noise."""

    def test_column_link_filtered(self) -> None:
        self.assertTrue(is_structural_noise("招标公告", "https://h.cn/notices/list-113.html"))
        self.assertTrue(is_structural_noise("科室导航", "https://h.cn/col123/"))

    def test_real_notice_not_filtered(self) -> None:
        self.assertFalse(is_structural_noise(
            "关于医院信息化建设项目的公开招标公告（2026年）",
            "https://h.cn/notice/12345.shtml"))


class CharsetTests(unittest.TestCase):
    """choose_charset prefers header, then meta, then content trial."""

    def test_header_wins(self) -> None:
        self.assertEqual(mc.choose_charset(b"<html>", "gbk"), "gbk")

    def test_meta_gb2312_maps_to_gb18030(self) -> None:
        self.assertEqual(mc.choose_charset(b'<meta charset="gb2312">', None), "gb18030")

    def test_valid_utf8_detected(self) -> None:
        self.assertEqual(mc.choose_charset("你好".encode("utf-8"), None), "utf-8")


if __name__ == "__main__":
    unittest.main()
