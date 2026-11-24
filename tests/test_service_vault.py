import os
import tempfile
import unittest
from pathlib import Path

from factory.provider_vault import ServiceVault, VaultError


@unittest.skipUnless(os.name=="posix","Requires POSIX permission and openat semantics")
class ServiceVaultTests(unittest.TestCase):
    def test_permissions_atomic_replace_workspace_separation_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory(prefix="gta-streamer-disposable-service-vault-") as folder:
            root = Path(folder)
            private = root/"credentials"
            private.mkdir(mode=0o700)
            vault = ServiceVault(root/"factory-state",private)
            self.assertTrue(vault.available)
            vault.put("indiepost-fixture","synthetic first value")
            vault.put("indiepost-fixture","synthetic replacement")
            self.assertEqual(vault.get("indiepost-fixture"),"synthetic replacement")
            self.assertIsNone(ServiceVault(root/"other-state",private).get("indiepost-fixture"))
            entry = private/vault.target("indiepost-fixture")
            self.assertEqual(entry.stat().st_mode & 0o777,0o600)
            entry.chmod(0o644)
            with self.assertRaises(VaultError):
                vault.get("indiepost-fixture")
            vault.remove("indiepost-fixture")
            visible = root/"not-a-credential"
            visible.write_text("protected")
            entry.symlink_to(visible)
            with self.assertRaises(VaultError):
                vault.get("indiepost-fixture")
            vault.remove("indiepost-fixture")
            self.assertEqual(visible.read_text(),"protected")
            with self.assertRaises(VaultError):
                vault.get("../escape")
