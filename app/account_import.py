"""Private credential parsing and safe account import, without browser actions."""
from __future__ import annotations

from collections import Counter
import hashlib
import re
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit


AUTO_PROXY_PORTS = (*range(20001, 20023), *range(20031, 20041))
DEFAULT_PROXIES = tuple(f"socks5://host.docker.internal:{port}" for port in AUTO_PROXY_PORTS)


def normalize_email(value: Any) -> str:
    email = str(value or "").strip().casefold()
    if (not re.fullmatch(r"[^\s@|]+@[^\s@|]+\.[^\s@|]+", email) or len(email) > 254
            or any(ord(char) < 32 or ord(char) == 127 for char in email)):
        raise ValueError("邮箱格式不正确")
    return email


def validate_proxy(value: str) -> str:
    # Return the explicit value unchanged after parsing; never normalize its exit.
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"socks5", "http", "https"} and parsed.hostname and parsed.port
                 and not parsed.username and not parsed.password and not parsed.path
                 and not parsed.query and not parsed.fragment
                 and not any(ord(char) <= 32 or ord(char) == 127 for char in value)
                 and "\\" not in value)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("代理需为带主机和端口的 socks5/http/https 地址，且不含认证参数")
    return value


def masked_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return local[:2] + "***@" + domain if domain else ""


def parse_import_lines(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    records, errors = [], []
    for number, line in enumerate(str(text or "").splitlines(), 1):
        if not line.strip():
            continue
        delimiter = "|" if "|" in line else "\t" if "\t" in line else "----" if "----" in line else "|"
        parts = line.split(delimiter)
        try:
            if len(parts) not in {2, 3}:
                raise ValueError("格式应为邮箱|密码|可选代理，或名称|代理；不接受额外列")
            first = parts[0].strip()
            if "@" in first:
                email = normalize_email(first)
                password = parts[1]  # Whitespace belongs to a password, never strip it.
                if not password or "\x00" in password:
                    raise ValueError("密码不能为空或包含空字符")
                proxy = parts[2].strip() if len(parts) == 3 else ""
                record = {"line": number, "kind": "credentials", "name": "ijm-" + hashlib.sha256(email.encode()).hexdigest()[:12],
                          "login_email": email, "login_password": password, "proxy_url": proxy,
                          "explicit_proxy": bool(proxy)}
            else:
                if len(parts) != 2 or not first or not parts[1].strip():
                    raise ValueError("旧格式应为名称|代理")
                record = {"line": number, "kind": "legacy", "name": first, "proxy_url": parts[1].strip(),
                          "explicit_proxy": True}
            if record["proxy_url"]:
                validate_proxy(record["proxy_url"])
            records.append(record)
        except ValueError as exc:
            errors.append(f"第 {number} 行：{exc}")
    return records, errors


class AccountImporter:
    def __init__(self, store: Any, *, login_busy: Callable[[int], bool] | None = None,
                 proxy_urls: Iterable[str] | None = None):
        self.store = store
        self.login_busy = login_busy or (lambda account_id: False)
        self.proxy_urls = tuple(validate_proxy(value) for value in (DEFAULT_PROXIES if proxy_urls is None else proxy_urls))

    def import_text(self, text: str) -> dict[str, Any]:
        records, errors = parse_import_lines(text)
        results, seen = [], set()
        saved = duplicates = 0
        for record in records:
            key = (record["kind"], record.get("login_email") or record["name"].casefold())
            if key in seen:
                duplicates += 1
                results.append({"line": record["line"], "status": "duplicate", "login_needed": False,
                                "message": "本批次重复账号，已忽略"})
                continue
            seen.add(key)
            try:
                existing = self.store.find_import_account(record.get("login_email", ""), record["name"])
                proxy = record["proxy_url"]
                if existing and existing["proxy_url"]:
                    if proxy and proxy != existing["proxy_url"]:
                        raise ValueError("已有账号代理不同，请先显式重置 Profile 后修改")
                    proxy = existing["proxy_url"]
                if not proxy:
                    if not self.proxy_urls:
                        raise ValueError("未配置可用代理")
                    counts = Counter(account["proxy_url"] for account in self.store.accounts(private=True))
                    proxy = min(enumerate(self.proxy_urls), key=lambda pair: (counts[pair[1]], pair[0]))[1]
                validate_proxy(proxy)
                account, changed = self.store.import_account({**record, "proxy_url": proxy},
                    busy=bool(existing and self.login_busy(int(existing["id"]))))
                if changed:
                    saved += 1
                else:
                    duplicates += 1
                results.append({"line": record["line"], "account_id": account["id"],
                    "status": "saved" if changed else "duplicate", "login_needed": changed and record["kind"] == "credentials",
                    "login_identifier": account.get("login_identifier", ""),
                    "message": "已保存，等待自动登录" if changed and record["kind"] == "credentials" else
                               "已保存，请完成网页登录" if changed else "已有相同账号，未修改"})
            except (ValueError, KeyError):
                # Store and callbacks may contain private values in exceptions.
                errors.append(f"第 {record['line']} 行：账号正在使用或信息与已有 Profile 不一致，请检查后重试")
                results.append({"line": record["line"], "status": "error", "login_needed": False,
                                "message": "导入未完成，请检查账号状态及代理"})
        return {"input_count": sum(bool(line.strip()) for line in str(text or "").splitlines()),
                "count": saved, "duplicate_count": duplicates, "login_started_count": 0,
                "results": results, "errors": errors}
