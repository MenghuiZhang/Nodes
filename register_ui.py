# -*- coding: utf-8 -*-
"""全程浏览器注册（UI 流程）。

与「打码走协议」的区别：注册表单在真实浏览器里填写并提交（Camoufox 有头），
邮箱/密码是真实值；Turnstile 在页面内点击通过；提交瞬间拦截 register XHR
响应拿到 access_token。之后的收码/验证/领试用/拉代理仍走 HTTP（复用既有函数）。

流程：
  1) YYDS 建临时邮箱
  2) Camoufox 打开 sign-up 页（屏蔽 Intercom 浮窗防遮挡）
  3) 键盘级逐字输入真实邮箱/密码（React 受控组件可靠接管），勾选条款
  4) 点击 Turnstile checkbox（一次），轮询 token
  5) 等 Continue 按钮解禁 → 提交
  6) 拦截 /auth/register 响应 → access_token + userData
  7) 关浏览器 → HTTP：resend → YYDS 收码 → verify → trial → 拉代理
"""

import time

import proxyscrape_register as worker


# 注册表单相关选择器（dashboard.proxyscrape.com/v2/sign-up，MUI 前端）
SEL_EMAIL = "input[type='email']"
SEL_PASSWORD = "#auth-login-v2-password"
SEL_CONFIRM = "#auth-login-v2-confirm-password"
SEL_TERMS = "input[type='checkbox']"
SEL_SUBMIT = "button[type='submit']"

# 屏蔽 Intercom 客服浮窗与 CookieBot 弹层：都会遮挡/劫持提交按钮点击
_HIDE_OVERLAYS_CSS = (
    "#intercom-container,.intercom-namespace,"
    "#cookiescript_injected,#onetrust-consent-sdk,#CybotCookiebotDialog"
    "{display:none!important}"
)


def _type_slowly(page, selector, value):
    """逐键输入：fill() 的程序化注入可能不被 React 受控组件接管。"""
    page.click(selector)
    page.fill(selector, "")
    page.type(selector, value, delay=45)


def _click_turnstile_once(page, deadline):
    """轮询 token；每 10s 未出就重新点击 checkbox（最多 8 次）。
    checkbox 中心实测在 widget 左侧约 49px（300x65 widget）。返回是否拿到 token。"""
    last_click = 0.0
    clicks = 0
    while time.time() < deadline:
        token = _read_turnstile_token(page)
        if len(token) >= 80:
            worker.log(f"Turnstile 通过，token 长度={len(token)}（共点击 {clicks} 次）")
            return True
        if clicks < 8 and time.time() - last_click >= 10.0:
            box = worker._challenge_box(page)
            if box and box.get("width", 0) >= 100 and box.get("height", 0) >= 40:
                x = int(box["x"] + min(49, box["width"] * 0.2))
                y = int(box["y"] + box["height"] / 2)
                try:
                    page.mouse.move(x - 80, y - 60, steps=6)
                    page.mouse.move(x, y, steps=8)
                    page.wait_for_timeout(120)
                    page.mouse.click(x, y)
                    clicks += 1
                    last_click = time.time()
                    worker.log(f"已点击 Turnstile checkbox ({x},{y}) 第 {clicks}/8 次")
                except Exception as exc:
                    worker.log(f"[!] 点击 Turnstile 失败: {exc}")
        page.wait_for_timeout(1000)
    return False


def _read_turnstile_token(page):
    try:
        raw = page.evaluate(
            "() => { const el = document.querySelector('input[name=\"cf-turnstile-response\"]');"
            " return el ? String(el.value||'') : ''; }"
        )
        return (raw or "").strip()
    except Exception:
        return ""


def register_ui_once(headless, node_file, ui_timeout=240):
    """单次 UI 注册：浏览器提交注册表单并拦截 access_token。
    成功返回 (record_partial, session, access_token, userdata)；失败抛异常。"""
    from camoufox.sync_api import Camoufox

    password = "Ps" + "".join(worker.random.choices(worker.string.ascii_letters + worker.string.digits, k=10)) + "!9"
    email, _ = worker.create_mailbox()

    captured = {}

    def on_response(response):
        try:
            if "/auth/register" in response.url and response.status == 200:
                data = response.json()
                if data.get("access_token"):
                    captured.update(data)
        except Exception:
            pass

    kwargs = {
        "headless": bool(headless),
        "os": ("macos" if worker.sys.platform == "darwin" else "linux"),
        "locale": "en-US",
        "humanize": True,
        "window": (1280, 900),
    }
    proxy_url = str(worker.os.environ.get("HTTPS_PROXY") or worker.os.environ.get("HTTP_PROXY") or "").strip()
    if proxy_url:
        kwargs["proxy"] = {"server": proxy_url}
        try:
            from camoufox.geolocation import geoip_allowed, get_geolocation

            geoip_allowed()
            exit_ip = worker._probe_exit_ip(proxy_url)
            if exit_ip:
                kwargs["geoip"] = exit_ip
        except Exception:
            pass

    deadline = time.time() + ui_timeout
    with Camoufox(**kwargs) as browser:
        page = browser.new_page()
        page.set_default_timeout(20000)
        page.on("response", on_response)
        try:
            page.add_init_script(
                "(() => { const s = document.createElement('style');"
                f" s.textContent = `{_HIDE_OVERLAYS_CSS}`;"
                " document.addEventListener('DOMContentLoaded', () => document.head.appendChild(s));"
                " if (document.head) document.head.appendChild(s); })()"
            )
        except Exception as exc:
            worker.log(f"[!] 遮挡屏蔽样式注入失败: {exc}")

        worker.log("Camoufox 打开 sign-up 页…")
        page.goto(worker.PS_SIGNUP_PAGE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector(SEL_EMAIL, timeout=30000)

        # ① 真实邮箱/密码，键盘级逐字输入
        _type_slowly(page, SEL_EMAIL, email)
        page.wait_for_timeout(300)
        _type_slowly(page, SEL_PASSWORD, password)
        page.wait_for_timeout(300)
        _type_slowly(page, SEL_CONFIRM, password)
        page.wait_for_timeout(300)
        if not page.is_checked(SEL_TERMS):
            page.check(SEL_TERMS)
        worker.log("表单已填写（真实邮箱）")

        # ② Turnstile：轮询 token，每 10s 未出就重新点击（最多 8 次）
        if not _click_turnstile_once(page, deadline):
            raise TimeoutError("Turnstile token 未下发（重试点击用尽）")
        worker.log(f"token 长度={len(_read_turnstile_token(page))}")

        # ③ 等 Continue 解禁后提交（Turnstile 通过前按钮 disabled）
        while time.time() < deadline:
            if not page.evaluate(
                "() => { const b = document.querySelector('button[type=submit]');"
                " return b ? b.disabled : true; }"
            ):
                break
            page.wait_for_timeout(400)
        page.evaluate("() => { const b = document.querySelector('button[type=submit]'); b && b.click(); }")
        worker.log("已提交注册表单")

        # ④ 等 register XHR 响应（拦截器写入 captured）
        while time.time() < deadline and not captured.get("access_token"):
            page.wait_for_timeout(500)
        if not captured.get("access_token"):
            body = ""
            try:
                body = page.evaluate("() => document.body ? document.body.innerText.slice(0, 200) : ''")
            except Exception:
                pass
            raise TimeoutError(f"未拦截到 register 响应（页面: {body[:120]}）")

    access_token = captured["access_token"]
    userdata = captured.get("userData") or {}
    worker.log("注册成功（UI），access_token 已拦截")
    record = {"email": email, "password": password}
    return record, email, password, access_token, userdata
