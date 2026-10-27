"""Nonsecret evidence for sharing a fresh Codex account probe within one poll.

Never open auth.json or read keychain values. Unknown configuration/policy falls
back to independent RPCs. This establishes a shared auth snapshot, never a payer.
"""

import ctypes
import json
import os
import re
import stat
import sys
from pathlib import Path

FIELDS = (
    "cli_auth_credentials_store",
    "chatgpt_base_url",
    "forced_chatgpt_workspace_id",
    "forced_login_method",
    "model_provider",
    "profile",
)


def version(path):
    s = path.stat()
    return (
        (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if stat.S_ISREG(s.st_mode)
        else None
    )


def mac_managed_preferences():
    if sys.platform != "darwin":
        return False
    try:
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFStringCreateWithCString.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32)
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFPreferencesCopyAppValue.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        cf.CFPreferencesCopyAppValue.restype = ctypes.c_void_p
        cf.CFRelease.argtypes = (ctypes.c_void_p,)
        cf.CFRelease.restype = None
        domain = cf.CFStringCreateWithCString(None, b"com.openai.codex", 0x08000100)
        if not domain:
            return True
        try:
            for name in (b"config_toml_base64", b"requirements_toml_base64"):
                key = cf.CFStringCreateWithCString(None, name, 0x08000100)
                if not key:
                    return True
                try:
                    value = cf.CFPreferencesCopyAppValue(key, domain)
                finally:
                    cf.CFRelease(key)
                if value:
                    cf.CFRelease(value)
                    return True  # Presence only; never decode policy values.
        finally:
            cf.CFRelease(domain)
        return False
    except (OSError, AttributeError, TypeError):
        return True


def managed(home):
    # Authentication can be forced before credential loading. Don't guess which
    # settings win or inspect managed payloads; any such source disables sharing.
    if os.name == "nt":
        return True
    try:
        for path in (
            Path("/etc/codex/managed_config.toml"),
            Path("/etc/codex/requirements.toml"),
            home / "managed_config.toml",
            home / "requirements.toml",
        ):
            if path.exists():
                return True
    except OSError:
        return True
    return mac_managed_preferences()


def balanced(line):
    quote = None
    escaped = False
    stack = []
    for char in line:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char == "#":
            break
        elif char in ('"', "'"):
            quote = char
        elif char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack or stack.pop() != ("[" if char == "]" else "{"):
                return False
    return not quote and not stack


def settings(home):
    """Read only selected literal root settings; unsupported TOML means no sharing."""
    result = {name: None for name in FIELDS}
    result["cli_auth_credentials_store"] = "file"
    path = home / "config.toml"
    try:
        before = version(path)
    except FileNotFoundError:
        return result
    except OSError:
        return None
    if before is None or before[2] > 256 * 1024:
        return None
    root = True
    seen = set()
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                if '"""' in text or "'''" in text or not balanced(text):
                    return None
                if text.startswith("["):
                    root = False
                    continue
                if not root:
                    continue
                match = re.match(
                    r'(?:("[^"\\]*"|\'[^\']*\')|([A-Za-z_][A-Za-z0-9_]*))\s*=\s*(.*)', text
                )
                if not match:
                    return None
                key = match[1][1:-1] if match[1] else match[2]
                raw = match[3]
                if key not in FIELDS:
                    continue
                if key in seen:
                    return None
                seen.add(key)
                if raw.startswith("'"):
                    end = raw.find("'", 1)
                    if end < 0:
                        return None
                    value = raw[1:end]
                    tail = raw[end + 1 :].strip()
                else:
                    try:
                        value, end = json.JSONDecoder().raw_decode(raw)
                    except ValueError:
                        return None
                    tail = raw[end:].strip()
                if not isinstance(value, str) or tail and not tail.startswith("#"):
                    return None
                result[key] = value
        if version(path) != before:
            return None
    except (OSError, UnicodeError):
        return None
    return result


def key(home):
    home = Path(home)
    # Environment authentication overrides and alternate config selection are
    # deliberately not interpreted, and their values never enter this module.
    if any(
        name in os.environ
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_ID_TOKEN_FILE")
    ):
        return None
    if any(
        name.startswith("CODEX_")
        and name != "CODEX_PERMISSION_PROFILE"
        and any(word in name for word in ("AUTH", "TOKEN", "CONFIG", "PROFILE"))
        for name in os.environ
    ):
        return None
    if managed(home):
        return None
    selected = settings(home)
    if selected is None or selected["cli_auth_credentials_store"] != "file" or selected["profile"]:
        return None
    try:
        auth = version(home / "auth.json")
    except OSError:
        return None
    return (auth, tuple(selected[name] for name in FIELDS)) if auth else None


def snapshot(home):
    """Shared key plus home-specific guards against symlink/config replacement."""
    home = Path(home)
    shared = key(home)
    if shared is None:
        return None

    def link(path):
        s = path.lstat()
        return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    try:
        config = home / "config.toml"
        guard = (
            link(home / "auth.json"),
            version(config) if config.exists() else None,
            link(config) if config.is_symlink() else None,
            link(home) if home.is_symlink() else None,
        )
    except OSError:
        return None
    return shared, guard
