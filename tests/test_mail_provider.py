import unittest
from unittest.mock import Mock, patch

import proxyscrape_register as register


class ExtractCodeTests(unittest.TestCase):
    """验证码提取：覆盖新旧邮件模板与异常样例。"""

    def test_new_template_your_code(self):
        # 2026-11 改版后的真实邮件正文
        text = ("VERIFY YOUR EMAIL Your code: 543337bd33 "
                "Enter this code to verify your email address.")
        self.assertEqual(register.extract_verification_code(text), "543337bd33")

    def test_old_template_verification_code(self):
        self.assertEqual(
            register.extract_verification_code("Your verification code: d253ff02f7"),
            "d253ff02f7",
        )

    def test_code_is_variant(self):
        self.assertEqual(
            register.extract_verification_code("Your ProxyScrape code is: Ab12Cd34Ef"),
            "Ab12Cd34Ef",
        )

    def test_html_wrapped(self):
        html = ("<div><span style=\"font-size:32px\">Your code:</span> "
                "<b>543337bd33</b></div>")
        self.assertEqual(register.extract_verification_code(html), "543337bd33")

    def test_chinese_template(self):
        self.assertEqual(
            register.extract_verification_code("您的验证码是：a1b2c3d4e5 请勿泄露"),
            "a1b2c3d4e5",
        )

    def test_no_code_returns_none(self):
        self.assertIsNone(
            register.extract_verification_code("Kind regards, The ProxyScrape team.")
        )

    def test_empty_returns_none(self):
        self.assertIsNone(register.extract_verification_code(""))
        self.assertIsNone(register.extract_verification_code(None))

    def test_fallback_mixed_token(self):
        # 短语全落空时，兜底找「字母+数字混合、8-16 位」的独立 token
        text = "Please use the token below to finish. 77f3ab19cd02 thanks"
        self.assertEqual(register.extract_verification_code(text), "77f3ab19cd02")

    def test_fallback_skips_plain_words_and_urls(self):
        # 纯字母单词 / 纯数字 / 长串 URL 都不应被兜底误抓
        text = "visit https://dashboard.proxyscrape.com/v2/v4/account for help"
        self.assertIsNone(register.extract_verification_code(text))


class MailProviderTests(unittest.TestCase):
    def test_yunxin_create_mailbox(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ok": True,
            "address": "ps123@mail.com",
            "domain": "mail.com",
            "kind": "mail",
        }
        with patch.object(register, "MAIL_API_KEY", "qm_test"), \
             patch.object(register, "MAIL_API_BASE", "https://mail.example"), \
             patch.object(register, "MAIL_TYPE", "mail"), \
             patch.object(register, "MAIL_SUFFIX", "mail.com"), \
             patch.object(register, "MAIL_DOMAIN", ""), \
             patch.object(register.requests, "post", return_value=response) as post:
            address, token = register.yunxin_create_mailbox()

        self.assertEqual(address, "ps123@mail.com")
        self.assertIsNone(token)
        self.assertEqual(post.call_args.kwargs["headers"]["X-API-Key"], "qm_test")
        self.assertEqual(post.call_args.kwargs["json"]["suffix"], "mail.com")

    def test_yunxin_wait_code_uses_codes_field(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ok": True,
            "results": [{"subject": "Verify", "codes": ["a1b2c3d4e5"]}],
        }
        with patch.object(register, "MAIL_API_KEY", "qm_test"), \
             patch.object(register, "MAIL_API_BASE", "https://mail.example"), \
             patch.object(register.requests, "get", return_value=response):
            code = register.yunxin_wait_code("ps123@mail.com", timeout=1, interval=0)

        self.assertEqual(code, "a1b2c3d4e5")

    def test_yunxin_cf_mailbox_does_not_send_suffix(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"ok": True, "address": "ps123@mail.example"}
        with patch.object(register, "MAIL_API_KEY", "qm_test"), \
             patch.object(register, "MAIL_API_BASE", "https://mail.example"), \
             patch.object(register, "MAIL_TYPE", "cf"), \
             patch.object(register, "MAIL_SUFFIX", "mail.com"), \
             patch.object(register, "MAIL_DOMAIN", "mail.example"), \
             patch.object(register.requests, "post", return_value=response) as post:
            register.yunxin_create_mailbox()

        payload = post.call_args.kwargs["json"]
        self.assertNotIn("suffix", payload)
        self.assertEqual(payload["domain"], "mail.example")

    def test_yunxin_wait_code_parses_message_text(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ok": True,
            "results": [{"subject": "ProxyScrape", "text": "Your verification code: z9y8x7w6"}],
        }
        with patch.object(register, "MAIL_API_KEY", "qm_test"), \
             patch.object(register, "MAIL_API_BASE", "https://mail.example"), \
             patch.object(register.requests, "get", return_value=response):
            code = register.yunxin_wait_code("ps123@mail.com", timeout=1, interval=0)

        self.assertEqual(code, "z9y8x7w6")

    def test_provider_dispatch_keeps_yyds_compatibility(self):
        with patch.object(register, "MAIL_PROVIDER", "yyds"), \
             patch.object(register, "yyds_create_mailbox", return_value=("a@example.com", "token")) as create:
            self.assertEqual(register.create_mailbox(), ("a@example.com", "token"))
            create.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
