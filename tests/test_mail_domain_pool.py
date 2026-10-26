import tempfile
import time
import pathlib
import unittest
from unittest.mock import Mock, patch

import proxyscrape_register as register
from mail_domain_pool import DomainPool, DomainPoolError, normalize_domain


class DomainPoolTests(unittest.TestCase):
    def setUp(self):
        self.pool = DomainPool(pathlib.Path(tempfile.mkdtemp()) / "pool.json")

    def _id(self, domain):
        return next(x["id"] for x in self.pool.list_public()["items"] if x["domain"] == domain)

    def test_add_and_list(self):
        result = self.pool.add(["a.com", "b.com", "bad_domain!"], enabled=True)
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["invalid"], 1)
        self.assertEqual(self.pool.summary(), {"total": 2, "enabled": 2, "disabled": 0})

    def test_normalize_domain(self):
        self.assertEqual(normalize_domain(" A.COM. "), "a.com")
        with self.assertRaises(DomainPoolError):
            normalize_domain("not a domain")

    def test_cooldown_after_consecutive_failures(self):
        self.pool.add(["a.com", "b.com"], enabled=True)
        for _ in range(3):
            self.pool.record_create_result("a.com", False, "timeout")
        row = self.pool.get_public(self._id("a.com"))
        self.assertGreater(row["cooldown_until"], time.time())
        self.assertEqual(row["consecutive_failures"], 3)
        # 冷却域名垫底
        self.assertEqual(self.pool.pick_domains()[-1], "a.com")
        # 成功清零
        self.pool.record_create_result("a.com", True)
        row = self.pool.get_public(self._id("a.com"))
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertEqual(row["cooldown_until"], 0.0)

    def test_register_result_by_email_lookup(self):
        self.pool.add(["b.com"], enabled=True)
        self.assertEqual(self.pool.record_register_success("user@b.com"), (1, 0))
        self.assertEqual(self.pool.record_register_failure("user@b.com", "x"), (1, 1))
        self.assertIsNone(self.pool.record_register_success("user@unknown.com"))

    def test_reconcile_preserves_history_and_disables_missing(self):
        self.pool.add(["a.com", "b.com"], enabled=True)
        self.pool.record_register_success("user@a.com")
        result = self.pool.reconcile_configured(["b.com", "c.com"])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["disabled"], 1)
        row_a = self.pool.get_public(self._id("a.com"))
        self.assertEqual(row_a["status"], "disabled")
        self.assertEqual(row_a["success_count"], 1)
        row_b = self.pool.get_public(self._id("b.com"))
        self.assertEqual(row_b["status"], "enabled")

    def test_reconcile_rejects_empty(self):
        with self.assertRaises(DomainPoolError):
            self.pool.reconcile_configured([])

    def test_update_toggle_and_delete(self):
        self.pool.add(["a.com"], enabled=True)
        row_id = self._id("a.com")
        item = self.pool.update(row_id, {"enabled": False})
        self.assertEqual(item["status"], "disabled")
        self.assertEqual(self.pool.delete([row_id]), {"removed": 1})
        self.assertEqual(self.pool.summary()["total"], 0)

    def test_persistence(self):
        self.pool.add(["a.com"], enabled=True)
        self.pool.record_register_success("user@a.com")
        reloaded = DomainPool(self.pool.path)
        self.assertEqual(reloaded.domain_counts("a.com"), (1, 0))


class CfTempRotationTests(unittest.TestCase):
    def setUp(self):
        register.CF_TEMP_BASE = "https://box.example.com"
        register.CF_TEMP_ADMIN_KEY = "k"
        register.CF_TEMP_DOMAIN = ""

    def test_manual_domain_wins(self):
        register.CF_TEMP_DOMAIN = "manual.com"
        self.assertEqual(register._cf_temp_domain_candidates(), ["manual.com"])
        register.CF_TEMP_DOMAIN = "auto"
        self.assertEqual(register._cf_temp_domain_candidates(), [""])

    def test_pool_candidates(self):
        with patch("mail_domain_pool.get_domain_pool") as gp:
            gp.return_value.pick_domains.return_value = ["p1.com", "p2.com"]
            self.assertEqual(register._cf_temp_domain_candidates(), ["p1.com", "p2.com"])

    def test_pool_empty_falls_back_to_auto(self):
        with patch("mail_domain_pool.get_domain_pool") as gp:
            gp.return_value.pick_domains.return_value = []
            self.assertEqual(register._cf_temp_domain_candidates(), [""])

    def test_rotation_on_failure(self):
        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"address": "x@p2.com", "jwt": "j1"}

        def fake_post(url, **kwargs):
            if kwargs.get("json", {}).get("domain") == "p1.com":
                raise RuntimeError("connect timeout")
            return Resp()

        with patch("mail_domain_pool.get_domain_pool") as gp, \
             patch.object(register, "requests") as rq:
            rq.post.side_effect = fake_post
            gp.return_value.pick_domains.return_value = ["p1.com", "p2.com"]
            email, jwt = register.cf_temp_create_mailbox()
        self.assertEqual(email, "x@p2.com")
        self.assertEqual(jwt, "j1")
        recorded = [(c.args[0], c.args[1]) for c in gp.return_value.record_create_result.call_args_list]
        self.assertEqual(recorded, [("p1.com", False), ("p2.com", True)])

    def test_all_candidates_fail(self):
        def fake_post(url, **kwargs):
            raise RuntimeError("down")

        with patch("mail_domain_pool.get_domain_pool") as gp, \
             patch.object(register, "requests") as rq:
            rq.post.side_effect = fake_post
            gp.return_value.pick_domains.return_value = ["p1.com"]
            with self.assertRaises(RuntimeError) as ctx:
                register.cf_temp_create_mailbox()
        self.assertIn("p1.com", str(ctx.exception))


class MailSettingsValidationTests(unittest.TestCase):
    _client = None

    @classmethod
    def _get_client(cls):
        import os
        if cls._client is None:
            os.environ["NODES_WEB_DATA_DIR"] = tempfile.mkdtemp()
            os.environ["NODES_CONFIG_FILE"] = os.path.join(tempfile.mkdtemp(), "config.local.json")
            import web_app
            web_app.app.config["TESTING"] = True
            web_app.app.config["SESSION_COOKIE_SECURE"] = False
            client = web_app.app.test_client()
            with client.session_transaction() as sess:
                sess["authenticated"] = True
                sess["username"] = web_app.WEB_USERNAME
                sess["csrf_token"] = "t"
            cls._client = client
        # 每个测试前清空配置文件，保证相互独立
        import web_app
        web_app.CONFIG_FILE.write_text("{}", encoding="utf-8")
        return cls._client

    def test_cf_temp_settings_roundtrip(self):
        client = self._get_client()
        r = client.put("/api/settings", headers={"X-CSRF-Token": "t"}, json={
            "group": "registration",
            "mail_provider": "cloudflare_temp",
            "cf_temp_base": "https://box.example.com",
            "cf_temp_admin_key": "admin123",
        })
        self.assertEqual(r.status_code, 200)
        r = client.get("/api/settings")
        settings = r.get_json()["settings"]
        self.assertEqual(settings["mail_provider"], "cloudflare_temp")
        self.assertEqual(settings["cf_temp_base"], "https://box.example.com")

    def test_cf_temp_requires_base_and_key(self):
        client = self._get_client()
        r = client.put("/api/settings", headers={"X-CSRF-Token": "t"}, json={
            "group": "registration",
            "mail_provider": "cloudflare_temp",
            "cf_temp_base": "",
            "cf_temp_admin_key": "",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("CloudTemp", r.get_json()["error"])

    def test_activate_validates_config(self):
        client = self._get_client()
        # 未配置 key 时切 yyds 被拒
        r = client.post("/api/mail/activate", headers={"X-CSRF-Token": "t"}, json={"provider": "yyds"})
        self.assertEqual(r.status_code, 400)
        # 配齐后成功
        client.put("/api/settings", headers={"X-CSRF-Token": "t"}, json={
            "group": "registration", "mail_provider": "yyds", "yyds_api_key": "k1",
        })
        r = client.post("/api/mail/activate", headers={"X-CSRF-Token": "t"}, json={"provider": "yyds"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["provider"], "yyds")

    def test_activate_rejects_unknown_provider(self):
        client = self._get_client()
        r = client.post("/api/mail/activate", headers={"X-CSRF-Token": "t"}, json={"provider": "nope"})
        self.assertEqual(r.status_code, 400)

    def test_pool_crud(self):
        client = self._get_client()
        h = {"X-CSRF-Token": "t"}
        r = client.post("/api/mail/pool", headers=h, json={"domains": ["a.com", "bad!"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["added"], 1)
        r = client.get("/api/mail/pool")
        self.assertEqual(r.get_json()["summary"]["total"], 1)
        item = r.get_json()["items"][0]
        r = client.patch(f"/api/mail/pool/{item['id']}", headers=h, json={"enabled": False})
        self.assertEqual(r.get_json()["item"]["status"], "disabled")
        r = client.post("/api/mail/pool/bulk", headers=h, json={"ids": [item["id"]], "action": "delete"})
        self.assertEqual(r.get_json()["removed"], 1)


if __name__ == "__main__":
    unittest.main()
