# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

"""Native macOS Keychain backend for Gajim passwords.

On macOS the bundled ``keyring`` package has no usable default backend
(``keyring.backends.macOS`` requires ``pyobjc-Security`` which is not
shipped), so Gajim otherwise falls back to storing passwords as clear text
in its config. This module implements a self-contained Keychain backend on
top of the pyobjc ``Security`` wrapper and is installed via
``keyring.set_keyring()`` from ``passwords.Interface.init()`` on darwin.

Keychain items are ``kSecClassGenericPassword`` records keyed by
``kSecAttrService="gajim"`` and ``kSecAttrAccount=<account jid>`` — the
same (service, username) pair upstream ``SecretPasswordStorage`` already
passes through, so no change to the password read/write/delete code is
needed.
"""

from __future__ import annotations

import logging

import keyring
from keyring.compat import properties

__all__ = ["install"]

log = logging.getLogger("gajim.c.keychain_darwin")


def install() -> keyring.backend.KeyringBackend | None:
    """Build the Keychain backend and return it.

    Returns ``None`` if the ``Security`` framework is not importable (e.g.
    a non-darwin host or a bundle without pyobjc-Security). The caller is
    responsible for ``keyring.set_keyring(...)``.
    """
    try:
        import Security  # noqa: F401
    except ImportError:
        log.warning("pyobjc-Security not available; native Keychain backend disabled")
        return None

    backend = MacOSKeychain()
    try:
        keyring.set_keyring(backend)
    except Exception:
        log.exception("Failed to install macOS Keychain backend")
        return None
    return backend


class MacOSKeychain(keyring.backend.KeyringBackend):
    """macOS Keychain keyring backend.

    Implements the ``keyring.backend.KeyringBackend`` interface using the
    Security framework (``SecItemAdd`` / ``SecItemCopyMatching`` /
    ``SecItemDelete``). Priority is set high so it wins over the bundled
    plaintext/fail backends; the classmethod raises if the framework is
    missing, which tells keyring to skip us.
    """

    SERVICE = "gajim"

    @properties.classproperty
    def priority(cls) -> int:
        # High priority so we are selected over the fail/plaintext backends.
        # keyring treats a raised exception here as "not viable".
        import Security  # noqa: F401

        return 10

    # -- KeyringBackend interface -----------------------------------------

    def set_password(self, service: str, username: str, password: str) -> None:
        # Upsert: delete any existing item for (service, account), then add.
        self._delete_item(service, username)
        self._add_item(service, username, password)

    def get_password(self, service: str, username: str) -> str | None:
        from CoreFoundation import kCFBooleanTrue
        from Security import errSecItemNotFound
        from Security import kSecAttrAccount
        from Security import kSecAttrService
        from Security import kSecClass
        from Security import kSecClassGenericPassword
        from Security import kSecMatchLimit
        from Security import kSecMatchLimitOne
        from Security import kSecReturnData
        from Security import SecItemCopyMatching

        query = {
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: username,
            kSecMatchLimit: kSecMatchLimitOne,
            kSecReturnData: kCFBooleanTrue,
        }

        error, result = SecItemCopyMatching(query, None)
        if error:
            if error == errSecItemNotFound:
                return None
            log.error("SecItemCopyMatching failed: %s", error)
            raise keyring.errors.PasswordSetError(f"Keychain read failed: {error}")

        if result is None:
            return None
        return self._cfdata_to_str(result)

    def delete_password(self, service: str, username: str) -> None:
        deleted = self._delete_item(service, username)
        if not deleted:
            raise keyring.errors.PasswordDeleteError(
                f"No Keychain item for {service}/{username}"
            )

    # -- internals --------------------------------------------------------

    @staticmethod
    def _add_item(service: str, username: str, password: str) -> None:
        from CoreFoundation import CFDataCreate
        from Foundation import kCFAllocatorDefault
        from Security import kSecAttrAccount
        from Security import kSecAttrService
        from Security import kSecClass
        from Security import kSecClassGenericPassword
        from Security import kSecValueData
        from Security import SecItemAdd

        encoded = password.encode("utf-8")
        value_data = CFDataCreate(kCFAllocatorDefault, encoded, len(encoded))

        attributes = {
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: username,
            kSecValueData: value_data,
        }

        error, _result = SecItemAdd(attributes, None)
        if error:
            log.error("SecItemAdd failed: %s", error)
            raise keyring.errors.PasswordSetError(f"Keychain write failed: {error}")

    @staticmethod
    def _delete_item(service: str, username: str) -> bool:
        from Security import errSecItemNotFound
        from Security import kSecAttrAccount
        from Security import kSecAttrService
        from Security import kSecClass
        from Security import kSecClassGenericPassword
        from Security import SecItemDelete

        query = {
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: service,
            kSecAttrAccount: username,
        }
        # SecItemDelete takes only the query and returns a bare OSStatus
        # (unlike SecItemAdd/SecItemCopyMatching, which also take a result
        # out-pointer and return a (status, result) tuple).
        error = SecItemDelete(query)
        if error == errSecItemNotFound:
            return False
        if error:
            log.error("SecItemDelete failed: %s", error)
            return False
        return True

    @staticmethod
    def _cfdata_to_str(cfdata) -> str:
        # CFDataRef is toll-free-bridged to NSData, which supports the buffer
        # protocol in pyobjc, so bytes() returns the raw bytes directly.
        return bytes(cfdata).decode("utf-8")
