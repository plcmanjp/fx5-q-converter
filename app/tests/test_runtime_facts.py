"""An unavailable private archive must never implicitly enable public mode."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from converter import distribution, runtime_facts
from converter.upstream_contracts import ContractValidationError


class RuntimeFactsTests(unittest.TestCase):
    def test_private_build_requires_explicit_public_switch(self):
        with patch.object(distribution, "PUBLIC_RELEASE", False):
            with self.assertRaises(ContractValidationError):
                runtime_facts.load_facts()

    def test_missing_and_tampered_facts_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(distribution, "PUBLIC_RELEASE", True), patch.object(distribution, "RUNTIME_FACTS_SHA256", "0" * 64):
                with self.assertRaises(ContractValidationError):
                    runtime_facts.load_facts(root)
                (root / "facts.json").write_bytes(b'{}')
                with self.assertRaises(ContractValidationError):
                    runtime_facts.load_facts(root)

    def test_version_binding_and_schema(self):
        value = dict(schema=runtime_facts.SCHEMA, version="2.2.63", source={}, target={}, records=[{}]*42,
                     finding_codes=["MINING_REQUIRED", "SOURCE_GLOBAL_ORDER_NOT_SERIALIZED"], authority_manifest_sha256="0"*64)
        body = json.dumps(value).encode()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "facts.json").write_bytes(body)
            with patch.object(distribution, "PUBLIC_RELEASE", True), patch.object(distribution, "RUNTIME_FACTS_SHA256", hashlib.sha256(body).hexdigest()):
                with patch("app.branding.application_version", return_value="2.2.63"):
                    self.assertEqual(value, runtime_facts.load_facts(root))
                with patch("app.branding.application_version", return_value="2.2.62"):
                    with self.assertRaises(ContractValidationError):
                        runtime_facts.load_facts(root)
