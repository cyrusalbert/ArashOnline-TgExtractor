"""Offline unit tests (no network, no sing-box needed).   Run:  python -m unittest discover -s tests -v"""
import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import TestResult  # noqa: E402
from src.parser import InvalidLink, b64decode_text, dedupe, extract_candidates, parse_candidates, parse_link  # noqa: E402
from src.renamer import apply_remark, flag_emoji, make_remark  # noqa: E402
from src.scraper import normalize_channel, parse_preview_page  # noqa: E402
from src.singbox import UnsupportedConfig, build_outbound  # noqa: E402

UID = "b831381d-6324-4d53-ad4f-8cda48b30811"


def vmess(**over):
    data = {"v": "2", "ps": "n", "add": "example.com", "port": "443", "id": UID, "aid": "0",
            "net": "ws", "host": "example.com", "path": "/ws", "tls": "tls"}
    data.update(over)
    return "vmess://" + base64.b64encode(json.dumps(data).encode()).decode()


class ParserTests(unittest.TestCase):
    def test_valid_links_of_every_protocol(self):
        ss = "ss://" + base64.urlsafe_b64encode(b"aes-256-gcm:pw").decode().rstrip("=") + "@1.2.3.4:8388#x"
        links = [vmess(), f"vless://{UID}@example.com:443?security=tls&type=grpc&serviceName=g#v",
                 "trojan://pw@example.com:443?sni=example.com#t", ss,
                 "hy2://pw@example.com:443?sni=example.com#h", f"tuic://{UID}:pw@example.com:443#u"]
        valid, rejected = parse_candidates(("c", l) for l in links)
        self.assertEqual(len(valid), 6, rejected)
        self.assertEqual({c.protocol for c in valid}, {"vmess", "vless", "trojan", "ss", "hysteria2", "tuic"})

    def test_malformed_links_are_rejected(self):
        bad = [vmess(id="nope"), vmess(port="70000"), vmess(add="127.0.0.1"), "vmess://!!!notbase64",
               f"vless://{UID}@:443", f"vless://{UID}@example.com:99999", "trojan://@example.com:443",
               "ss://bad", f"tuic://{UID}@example.com:443", "hy2://@example.com:443",
               f"vless://{UID}@example.com:443?security=reality&pbk=short"]
        for link in bad:
            with self.assertRaises(InvalidLink, msg=link):
                parse_link(link)

    def test_extraction_handles_escaping_code_blocks_and_glued_links(self):
        v = f"vless://{UID}@example.com:443?type=ws&amp;security=tls&amp;path=%2Fx#a"
        t = "trojan://pw@example.com:443?sni=example.com#t"
        text = f"<code>{v}</code><br/>text {t}{vmess()} (end)"
        found = extract_candidates(text)
        self.assertEqual(len(found), 3)
        self.assertIn("&security=tls", found[0])  # &amp; was decoded

    def test_dedupe_ignores_remark_only(self):
        a, b = parse_link(vmess(ps="A")), parse_link(vmess(ps="B"))
        c = parse_link(vmess(path="/other"))
        self.assertEqual(len(dedupe([a, b, c])), 2)

    def test_b64_padding_variants(self):
        self.assertEqual(b64decode_text("YWJj"), "abc")
        self.assertEqual(b64decode_text("YWI"), "ab")
        self.assertEqual(b64decode_text("YWI="), "ab")


class RenamerTests(unittest.TestCase):
    def _result(self, link):
        r = TestResult(config=parse_link(link))
        r.alive, r.ping_ms, r.speed_mbps, r.country_code = True, 85, 12.43, "DE"
        return r

    def test_remark_format_and_flag(self):
        r = self._result(vmess())
        self.assertEqual(flag_emoji("DE"), "\U0001F1E9\U0001F1EA")
        self.assertEqual(make_remark(r, 1, "{flag} {cc} | {ping}ms | {speed}Mbps | #{rank:03d}"),
                         "\U0001F1E9\U0001F1EA DE | 85ms | 12.4Mbps | #001")

    def test_vmess_rename_keeps_everything_else(self):
        out = apply_remark(vmess(), "vmess", "NEW NAME")
        data = json.loads(b64decode_text(out[len("vmess://"):]))
        self.assertEqual((data["ps"], data["id"], data["add"], data["path"]), ("NEW NAME", UID, "example.com", "/ws"))

    def test_fragment_rename_is_percent_encoded(self):
        out = apply_remark("trojan://pw@example.com:443?sni=x#old", "trojan", "DE | 5ms")
        self.assertEqual(out, "trojan://pw@example.com:443?sni=x#DE%20%7C%205ms")


class ScraperTests(unittest.TestCase):
    def test_channel_normalisation(self):
        for raw in ("@name_one", "https://t.me/name_one", "t.me/s/name_one", "name_one  # c"):
            self.assertEqual(normalize_channel(raw), "name_one")
        self.assertIsNone(normalize_channel("# only a comment"))

    def test_preview_page_parsing(self):
        html = ('<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="chan/42">'
                '<div class="tgme_widget_message_text">hi <code>trojan://pw@example.com:443#t</code></div></div></div>')
        msgs = parse_preview_page(html, "chan")
        self.assertEqual((len(msgs), msgs[0].msg_id), (1, 42))
        self.assertEqual(len(extract_candidates(msgs[0].text)), 1)


class SingBoxTests(unittest.TestCase):
    def test_outbounds(self):
        ob = build_outbound(parse_link(vmess()))
        self.assertEqual((ob["type"], ob["transport"]["type"], ob["tls"]["server_name"]), ("vmess", "ws", "example.com"))
        ob = build_outbound(parse_link(f"tuic://{UID}:pw@example.com:443#u"))
        self.assertEqual((ob["type"], ob["tls"]["alpn"]), ("tuic", ["h3"]))
        with self.assertRaises(UnsupportedConfig):
            build_outbound(parse_link(f"vless://{UID}@example.com:443?type=kcp&security=tls"))


if __name__ == "__main__":
    unittest.main()
