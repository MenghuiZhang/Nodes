import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import web_app as target


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.account_dir = root / "account"
        self.node_dir = root / "node"
        self.account_dir.mkdir()
        self.node_dir.mkdir()
        self.patches = [
            patch.object(target, "ACCOUNT_DIR", self.account_dir),
            patch.object(target, "NODE_DIR", self.node_dir),
        ]
        for item in self.patches:
            item.start()
        target.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = target.app.test_client()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def authenticate(self):
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = target.WEB_USERNAME
            session["csrf_token"] = "test-csrf"

    def test_health_does_not_require_login(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")

    def test_favicon_is_public_and_not_redirected(self):
        for path, mimetype in (
            ("/favicon.ico", "image/vnd.microsoft.icon"),
            ("/apple-touch-icon.png", "image/png"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertTrue(response.mimetype.startswith(mimetype), response.mimetype)
            self.assertGreater(len(response.data), 100, path)

    def test_pages_link_the_icon_set(self):
        for path in ("/login",):
            body = self.client.get(path).get_data(as_text=True)
            self.assertIn('rel="icon"', body)
            self.assertIn("favicon.ico", body)
            self.assertIn("apple-touch-icon", body)

    def test_fonts_are_self_hosted(self):
        """CSP forbids third-party stylesheets, so fonts must be local."""
        # Dashboard (needs a session) and login (needs none) both load the same CSS.
        login_body = self.client.get("/login").get_data(as_text=True)
        self.authenticate()
        index_body = self.client.get("/").get_data(as_text=True)
        for path, body in (("/login", login_body), ("/", index_body)):
            self.assertNotIn("fonts.googleapis.com", body, path)
            self.assertNotIn("fonts.gstatic.com", body, path)
            self.assertIn("fonts/fonts.css", body, path)

    def test_local_font_assets_are_served(self):
        css = self.client.get("/static/fonts/fonts.css")
        self.assertEqual(css.status_code, 200)
        self.assertTrue(css.mimetype.startswith("text/css"), css.mimetype)
        text = css.get_data(as_text=True)
        # Every advertised file must exist and be served with a font MIME type.
        files = sorted(set(re.findall(r"url\('([^']+\.woff2)'\)", text)))
        self.assertGreaterEqual(len(files), 8)
        for name in files:
            asset = self.client.get(f"/static/fonts/{name}")
            self.assertEqual(asset.status_code, 200, name)
            self.assertTrue(asset.mimetype.startswith("font/"), f"{name} {asset.mimetype}")
            self.assertGreater(len(asset.data), 1000, name)

    def test_dashboard_requires_login(self):
        self.assertEqual(self.client.get("/api/dashboard").status_code, 401)

    def test_accounts_list_hides_secrets_detail_shows_them(self):
        record = {
            "email": "masked@example.com",
            "password": "do-not-return",
            "access_token": "do-not-return-either",
            "account_id": "acc-1",
            "proxy_username": "user1",
            "proxy_password": "pass1",
            "verified": True,
            "trial_claimed": True,
            "proxy_count": 100,
            "ts": 1,
            "api_key": {
                "id": "key-1",
                "token": "public-api-token",
                "name": "nodes",
                "permissions": ["datacenter_shared:read"],
            },
        }
        (self.account_dir / "accounts_test.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.authenticate()

        listed = self.client.get("/api/accounts")
        rendered = listed.get_data(as_text=True)
        self.assertEqual(listed.status_code, 200)
        self.assertIn("masked@example.com", rendered)
        self.assertNotIn("do-not-return", rendered)
        self.assertNotIn("public-api-token", rendered)
        self.assertTrue(listed.json["accounts"][0]["has_api_key"])

        detail = self.client.get("/api/accounts/masked@example.com")
        self.assertEqual(detail.status_code, 200)
        account = detail.json["account"]
        self.assertEqual(account["password"], "do-not-return")
        self.assertEqual(account["api_token"], "public-api-token")
        self.assertIn("datacenter_shared:read", account["permissions"])
        self.assertIn("/v4/account/acc-1/datacenter_shared/proxy-list", account["api"]["public_proxy_list"])

    def test_account_update_persists_password_and_permissions(self):
        record = {
            "email": "edit@example.com",
            "password": "old-pass",
            "verified": True,
            "ts": 1,
        }
        (self.account_dir / "accounts_test.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.authenticate()

        response = self.client.put(
            "/api/accounts/edit@example.com",
            json={"password": "new-pass", "permissions": ["datacenter_shared:read", "subaccount:read"]},
            headers={"X-CSRF-Token": "test-csrf"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["account"]["password"], "new-pass")
        self.assertEqual(response.json["account"]["permissions"], ["datacenter_shared:read", "subaccount:read"])
        saved = (self.account_dir / "accounts_edits.jsonl").read_text(encoding="utf-8")
        self.assertIn("new-pass", saved)

    def test_task_creation_requires_csrf(self):
        self.authenticate()
        response = self.client.post("/api/tasks", json={"count": 1, "concurrency": 1})
        self.assertEqual(response.status_code, 403)

    @patch.object(target.TASK_STORE, "start_task")
    def test_task_creation_validates_and_starts(self, start):
        start.return_value = {"id": "task-1", "status": "queued"}
        self.authenticate()

        response = self.client.post(
            "/api/tasks",
            json={"count": 2, "concurrency": 2},
            headers={"X-CSRF-Token": "test-csrf"},
        )

        self.assertEqual(response.status_code, 202)
        start.assert_called_once_with(2, 2)


class TaskStoreTests(unittest.TestCase):
    @patch.object(target.worker, "register_one")
    def test_task_lifecycle_reaches_success(self, register_one):
        register_one.return_value = {"proxy_count": 100}
        with tempfile.TemporaryDirectory() as directory:
            store = target.TaskStore(Path(directory) / "tasks.json")

            task = store.start_task(2, 2)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = store.get(task["id"])
                if current["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.01)

            self.assertEqual(current["status"], "success")
            self.assertEqual(current["completed"], 2)
            self.assertEqual(current["successes"], 2)
            self.assertEqual(current["proxy_count"], 200)
            self.assertEqual(register_one.call_count, 2)


class AccountOpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.account_dir = root / "account"
        self.node_dir = root / "node"
        self.account_dir.mkdir()
        self.node_dir.mkdir()
        self.patches = [
            patch.object(target, "ACCOUNT_DIR", self.account_dir),
            patch.object(target, "NODE_DIR", self.node_dir),
        ]
        for item in self.patches:
            item.start()
        target.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = target.app.test_client()
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = target.WEB_USERNAME
            session["csrf_token"] = "test-csrf"

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def headers(self):
        return {"X-CSRF-Token": "test-csrf"}

    def write_account(self, record, name="accounts_test.jsonl"):
        path = self.account_dir / name
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def test_accounts_list_includes_expiry_and_remaining_bandwidth(self):
        expiry = int(time.time()) + 7 * 86400 + 3600
        self.write_account({
            "email": "usage@example.com",
            "verified": True,
            "proxy_count": 100,
            "ts": 1,
            "expiration_time": expiry,
            "bandwidth_total": 10000000000,
            "bandwidth_used": 2500000000,
            "bandwidth_remaining": 7500000000,
            "days_remaining": 7,
        })
        listed = self.client.get("/api/accounts")
        self.assertEqual(listed.status_code, 200)
        account = listed.json["accounts"][0]
        self.assertEqual(account["email"], "usage@example.com")
        self.assertEqual(account["expiry"], expiry)
        self.assertTrue(account["expires_at"])
        self.assertEqual(account["bandwidth_remaining"], 7500000000)
        self.assertFalse(account["expired"])
        self.assertNotIn("password", account)

    def test_delete_hides_account_and_import_restores_it(self):
        self.write_account({
            "email": "keep@example.com",
            "password": "secret",
            "verified": True,
            "ts": 1,
        })
        deleted = self.client.post(
            "/api/accounts/delete",
            json={"emails": ["keep@example.com"]},
            headers=self.headers(),
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json["deleted"], ["keep@example.com"])
        self.assertEqual(self.client.get("/api/accounts").json["accounts"], [])

        imported = self.client.post(
            "/api/accounts/import",
            json={"text": json.dumps({"email": "keep@example.com", "password": "secret", "verified": True})},
            headers=self.headers(),
        )
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json["imported"], 1)
        self.assertEqual(imported.json["restored"], 1)
        emails = [item["email"] for item in self.client.get("/api/accounts").json["accounts"]]
        self.assertEqual(emails, ["keep@example.com"])

    @patch.object(target.worker, "list_proxy_hosts", return_value=["10.0.0.1:10000", "10.0.0.2:10000"])
    @patch.object(target.worker, "fetch_accounts_summary", return_value=[])
    @patch.object(target.worker, "fetch_service_overview")
    def test_sync_usage_persists_expiry_and_remaining(self, overview, _summary, _hosts):
        expiry = int(time.time()) + 8 * 86400
        overview.return_value = {
            "bandwidth": 10000000000,
            "bandwidth_used": 1000000000,
            "bandwidth_period_end": expiry,
            "key": "account-key",
            "is_trial": True,
            "services": {
                "datacenter_shared": {
                    "proxy_username": "user1",
                    "proxy_password": "pass1",
                    "expiration_time": expiry,
                    "proxy_amount": 100,
                }
            },
        }
        self.write_account({
            "email": "sync@example.com",
            "access_token": "token",
            "account_id": "acc-1",
            "verified": True,
            "ts": 1,
        })
        response = self.client.post(
            "/api/accounts/sync-usage",
            json={"emails": ["sync@example.com"]},
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["synced"], ["sync@example.com"])
        account = response.json["accounts"][0]
        self.assertEqual(account["bandwidth_total"], 10000000000)
        self.assertEqual(account["bandwidth_used"], 1000000000)
        self.assertEqual(account["bandwidth_remaining"], 9000000000)
        self.assertEqual(account["expiry"], expiry)
        self.assertGreaterEqual(account["days_remaining"], 7)
        saved = (self.account_dir / "accounts_edits.jsonl").read_text(encoding="utf-8")
        self.assertIn("10.0.0.1:10000", saved)

    def test_overview_credentials_computes_remaining_traffic(self):
        expiry = int(time.time()) + 7 * 86400 + 3600
        creds = target.worker.overview_credentials({
            "bandwidth": 10000000000,
            "bandwidth_used": 2500000000,
            "bandwidth_period_end": expiry,
            "services": {
                "datacenter_shared": {
                    "proxy_username": "u",
                    "proxy_password": "p",
                    "expiration_time": expiry,
                    "proxy_amount": 100,
                }
            },
        })
        self.assertEqual(creds["bandwidth_remaining"], 7500000000)
        self.assertEqual(creds["expiration_time"], expiry)
        self.assertEqual(creds["days_remaining"], 7)


class PoolExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.account_dir = root / "account"
        self.node_dir = root / "node"
        self.account_dir.mkdir()
        self.node_dir.mkdir()
        self.patches = [
            patch.object(target, "ACCOUNT_DIR", self.account_dir),
            patch.object(target, "NODE_DIR", self.node_dir),
            # 质量门默认开启会真实联网探测 slot（假 IP 必超时全拒），
            # 导出用例只关心格式与数量，这里显式关闭。
            patch.object(target, "_config_for_quality",
                         lambda *args, **kwargs: {"proxy_quality_enabled": False}),
        ]
        for item in self.patches:
            item.start()
        target.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = target.app.test_client()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def write_account(self, record):
        path = self.account_dir / "accounts_test.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def live_record(self, email, hosts, **extra):
        record = {
            "email": email,
            "verified": True,
            "proxy_username": extra.pop("proxy_username", email.split("@")[0]),
            "proxy_password": "pw",
            "proxy_ips": hosts,
            "expiration_time": extra.pop("expiration_time", int(time.time()) + 7 * 86400),
            "bandwidth_remaining": extra.pop("bandwidth_remaining", 9 * 1024 * 1024 * 1024),
            "ts": 1,
        }
        record.update(extra)
        return record

    def test_live_proxies_requires_token(self):
        response = self.client.get("/api/export/live-proxies")
        self.assertEqual(response.status_code, 401)

    def test_live_proxies_emits_eight_ips_per_live_account(self):
        hosts = [f"10.1.1.{index}:10000" for index in range(1, 13)]
        self.write_account(self.live_record("one@example.com", hosts, proxy_username="user-one"))
        self.write_account(self.live_record(
            "dead@example.com",
            ["9.9.9.9:1"],
            proxy_username="dead",
            expiration_time=int(time.time()) - 10,
        ))
        response = self.client.get("/api/export/live-proxies?token=test-export-token")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith("text/plain"))
        lines = [line for line in response.get_data(as_text=True).splitlines() if line.strip()]
        self.assertEqual(len(lines), 8)
        self.assertTrue(all(line.startswith("http://user-one:pw@") for line in lines))
        self.assertTrue(all("9.9.9.9" not in line for line in lines))
        again = self.client.get("/api/export/live-proxies?token=test-export-token")
        self.assertEqual(again.get_data(as_text=True), response.get_data(as_text=True))

    def test_live_proxies_harvests_missing_ips_from_node_files(self):
        self.write_account(self.live_record("two@example.com", [], proxy_username="user-two", proxy_ips=[]))
        (self.node_dir / "proxies_old.txt").write_text(
            "\n".join(f"http://user-two:pw@11.1.1.{index}:8080" for index in range(1, 10)) + "\n",
            encoding="utf-8",
        )
        response = self.client.get("/api/export/live-proxies?token=test-export-token")
        self.assertEqual(response.status_code, 200)
        lines = [line for line in response.get_data(as_text=True).splitlines() if line.strip()]
        self.assertEqual(len(lines), 8)
        self.assertTrue(all("user-two:pw@11.1.1." in line for line in lines))

    def test_live_proxies_scales_eight_slots_per_account(self):
        for index in range(1, 3):
            hosts = [f"10.{index}.1.{slot}:10000" for slot in range(1, 12)]
            self.write_account(self.live_record(
                f"acc{index}@example.com",
                hosts,
                proxy_username=f"user-{index}",
            ))
        response = self.client.get("/api/export/live-proxies?token=test-export-token")
        lines = [line for line in response.get_data(as_text=True).splitlines() if line.strip()]
        self.assertEqual(len(lines), 16)
        self.assertEqual(sum(1 for line in lines if "user-1:pw@" in line), 8)
        self.assertEqual(sum(1 for line in lines if "user-2:pw@" in line), 8)

    @patch.object(target.pool, "resin_auth", return_value=("gw-token", "V1"))
    def test_gpt_gateway_matches_live_slot_count(self, _auth):
        for index in range(1, 3):
            hosts = [f"10.{index}.1.{slot}:10000" for slot in range(1, 12)]
            self.write_account(self.live_record(
                f"acc{index}@example.com",
                hosts,
                proxy_username=f"user-{index}",
            ))
        response = self.client.get("/api/export/gpt-gateway?token=test-export-token")
        self.assertEqual(response.status_code, 200)
        lines = [line for line in response.get_data(as_text=True).splitlines() if line.strip()]
        self.assertEqual(len(lines), 16)
        self.assertTrue(all(":8970" in line for line in lines))
        self.assertTrue(all("Nodes.n" in line for line in lines))
        self.assertIn("Nodes.n01:", lines[0])
        self.assertIn("Nodes.n16:", lines[-1])

    @patch.object(target.pool, "resin_auth", return_value=("gw-token", "V1"))
    def test_clash_export_matches_live_slot_count(self, _auth):
        for index in range(1, 3):
            hosts = [f"10.{index}.1.{slot}:10000" for slot in range(1, 12)]
            self.write_account(self.live_record(
                f"acc{index}@example.com",
                hosts,
                proxy_username=f"user-{index}",
            ))
        denied = self.client.get("/api/export/clash.yml")
        self.assertEqual(denied.status_code, 401)
        response = self.client.get("/api/export/clash.yml?token=test-export-token")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("type: http", body)
        self.assertNotIn("type: socks5", body)
        self.assertNotIn('server: "10.1.1.', body)
        self.assertIn('name: "Nodes-01"', body)
        self.assertIn('name: "Nodes-16"', body)
        self.assertEqual(body.count("type: http"), 16)
        self.assertIn('username: "Nodes.n01"', body)

    def test_clash_yaml_renders_gateway_slots_with_url_test_group(self):
        body = target.pool.clash_yaml(
            2, "gw-token", "198.18.0.1", 8970, "V1", "Nodes", protocol="http",
        )
        self.assertIn("type: http", body)
        self.assertNotIn("type: socks5", body)
        self.assertIn('server: "198.18.0.1"', body)
        self.assertIn('name: "Nodes-01"', body)
        self.assertIn('name: "Nodes-02"', body)
        self.assertIn('username: "Nodes.n01"', body)
        self.assertIn("type: url-test", body)

    @patch.object(target.pool, "resin_auth", return_value=("gw-token", "V1"))
    def test_clash_export_accepts_query_token_even_with_dummy_bearer(self, _auth):
        hosts = [f"10.1.1.{slot}:10000" for slot in range(1, 12)]
        self.write_account(self.live_record("acc1@example.com", hosts, proxy_username="user-1"))
        response = self.client.get(
            "/api/export/clash.yml?token=test-export-token",
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("filename=\"clash.yml\"", response.headers.get("Content-Disposition", ""))
        path_ok = self.client.get("/api/export/clash.yml/test-export-token")
        self.assertEqual(path_ok.status_code, 200)
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = target.WEB_USERNAME
        logged_in = self.client.get("/api/export/clash.yml")
        self.assertEqual(logged_in.status_code, 200)

    @patch.object(target.pool, "resin_auth", return_value=("gw-token", "V1"))
    def test_ladder_export_is_base64_uri_list(self, _auth):
        import base64
        hosts = [f"10.1.1.{slot}:10000" for slot in range(1, 12)]
        self.write_account(self.live_record("acc1@example.com", hosts, proxy_username="user-1"))
        response = self.client.get("/api/export/ladder?token=test-export-token")
        self.assertEqual(response.status_code, 200)
        decoded = base64.b64decode(response.get_data(as_text=True).strip()).decode("utf-8")
        lines = [line for line in decoded.splitlines() if line.strip()]
        self.assertEqual(len(lines), 8)
        self.assertTrue(all(line.startswith("http://Nodes.n") for line in lines))
        self.assertTrue(all(":8970#" in line for line in lines))

    @patch.object(target.TASK_STORE, "active", return_value=None)
    @patch.object(target.TASK_STORE, "start_task")
    def test_ensure_capacity_starts_register_task_when_short(self, start, _active):
        start.return_value = {"id": "task-fill", "status": "queued"}
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = target.WEB_USERNAME
            session["csrf_token"] = "test-csrf"
        response = self.client.post(
            "/api/pool/ensure-capacity",
            json={"auto_register": True},
            headers={"X-CSRF-Token": "test-csrf"},
        )
        self.assertEqual(response.status_code, 200)
        start.assert_called_once_with(5, 1)
        self.assertEqual(response.json["task"]["id"], "task-fill")


class PoolCapacitySettingsTests(unittest.TestCase):
    """容量与补号设置：配置落盘 + 开关往返 + 运行中仍可切换。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config_file = root / "config.local.json"
        self.config_file.write_text("{}", encoding="utf-8")
        self.patches = [
            patch.object(target, "CONFIG_FILE", self.config_file),
            patch.object(target, "WEB_DATA_DIR", root / "web-data"),
        ]
        for item in self.patches:
            item.start()
        target.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        self.client = target.app.test_client()
        with self.client.session_transaction() as session:
            session["authenticated"] = True
            session["username"] = target.WEB_USERNAME
            session["csrf_token"] = "test-csrf"

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def headers(self):
        return {"X-CSRF-Token": "test-csrf"}

    def saved_config(self):
        return json.loads(self.config_file.read_text(encoding="utf-8"))

    def test_defaults_are_exposed(self):
        response = self.client.get("/api/settings")
        self.assertEqual(response.status_code, 200)
        settings = response.json["settings"]
        self.assertIs(settings["pool_auto_register"], True)
        self.assertEqual(settings["pool_target_slots"], 80)
        self.assertEqual(settings["pool_slots_per_account"], 8)

    def test_toggle_auto_register_persists_and_reads_back(self):
        off = self.client.put(
            "/api/settings", json={"group": "pool", "pool_auto_register": False}, headers=self.headers(),
        )
        self.assertEqual(off.status_code, 200)
        self.assertIs(off.json["settings"]["pool_auto_register"], False)
        self.assertIs(self.saved_config()["pool_auto_register"], False)

        settings = self.client.get("/api/settings?group=pool").json["settings"]
        self.assertIs(settings["pool_auto_register"], False)

        on = self.client.put(
            "/api/settings", json={"group": "pool", "pool_auto_register": True}, headers=self.headers(),
        )
        self.assertEqual(on.status_code, 200)
        self.assertIs(on.json["settings"]["pool_auto_register"], True)

    def test_dashboard_pool_reflects_auto_register(self):
        self.client.put(
            "/api/settings", json={"group": "pool", "pool_auto_register": False}, headers=self.headers(),
        )
        pool_row = self.client.get("/api/dashboard").json["pool"]
        self.assertIs(pool_row["auto_register"], False)
        self.assertEqual(pool_row["target_slots"], 80)

    def test_capacity_fields_validate_and_clamp(self):
        response = self.client.put(
            "/api/settings",
            json={"group": "pool", "pool_slots_per_account": 9},
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.put(
            "/api/settings",
            json={"group": "pool", "pool_target_slots": 3, "pool_slots_per_account": 8},
            headers=self.headers(),
        )
        self.assertEqual(response.status_code, 200)
        # 目标槽位不能低于每号槽位，否则巡逻会无限补号。
        self.assertEqual(response.json["settings"]["pool_target_slots"], 8)

    @patch.object(target.TASK_STORE, "active", return_value={"id": "task-running"})
    def test_pool_group_is_editable_while_task_runs(self, _active):
        response = self.client.put(
            "/api/settings", json={"group": "pool", "pool_auto_register": False}, headers=self.headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json["settings"]["pool_auto_register"], False)

    @patch.object(target.TASK_STORE, "active", return_value={"id": "task-running"})
    def test_other_groups_still_locked_while_task_runs(self, _active):
        response = self.client.put(
            "/api/settings", json={"group": "quality", "proxy_max_latency_ms": 2500}, headers=self.headers(),
        )
        self.assertEqual(response.status_code, 409)


class MailboxLocalTests(unittest.TestCase):
    def test_random_mailbox_local_is_not_serial_ps_prefix(self):
        samples = [target.worker.random_mailbox_local() for _ in range(200)]
        self.assertEqual(len(samples), 200)
        self.assertEqual(len(set(samples)), 200)
        prefixes = {}
        lengths = set()
        for local in samples:
            self.assertTrue(re.fullmatch(r"[a-z][a-z0-9]{7,15}", local), local)
            self.assertFalse(local.startswith(("ps", "test", "admin", "user", "mail", "temp", "tmp")))
            prefixes[local[:2]] = prefixes.get(local[:2], 0) + 1
            lengths.add(len(local))
        self.assertGreaterEqual(len(prefixes), 20)
        self.assertGreaterEqual(len(lengths), 2)
        self.assertLess(max(prefixes.values()) / len(samples), 0.25)


if __name__ == "__main__":
    unittest.main()
