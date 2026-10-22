"""Synthetic incomplete source must stay blocked and retain useful evidence."""
from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from converter import core, text_conversion
from app import workflow_service


class SourceDiagnosticsTests(unittest.TestCase):
    def test_incomplete_program_reports_coverage_and_reasons(self):
        body = b"synthetic GX3"
        value = {
            "input": {"sha256": hashlib.sha256(body).hexdigest()},
            "coverage": {"pou": dict(total=2, decoded=1, partial=1, unknown=0),
                         "record": dict(total=4, decoded=2, partial=0, unknown=2)},
            "pous": [{"name": "SYNTHETIC", "findings": [
                {"object_kind": "opcode", "reason": "unsupported signature"}]}],
            "findings": [],
        }
        with patch.object(core, "_require_source_contract"), patch.object(core, "_validated_public_upstream"), \
             patch.object(text_conversion.upstream_contracts, "build_source_neutral_ir", return_value=value):
            with self.assertRaises(text_conversion.SourceProgramDecodeError) as caught:
                text_conversion._fx5_document(Path("synthetic.gx3"), body, None)
        error = caught.exception
        self.assertIn("SYNTHETIC", error.reason)
        detail = json.loads(error.detail)
        self.assertEqual(detail["coverage"]["record"]["unknown"], 2)
        self.assertEqual(detail["programs"][0]["findings"][0]["reason"], "unsupported signature")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "synthetic.gx3"
            source.write_bytes(body)
            request = workflow_service.WorkflowRequest("FX5_TO_Q", source, options={"complete_text": True})
            with patch.object(text_conversion, "analyze_text_source", side_effect=error):
                result = workflow_service.analyze(request)
        self.assertIsNone(result.source_identity)
        self.assertEqual(result.programs, ())
        self.assertEqual(result.diagnostics[0].code, "TEXT_SOURCE_PROGRAM_UNDECODED")
        self.assertEqual(result.diagnostics[0].detail, error.detail)
        self.assertEqual(result.diagnostics[0].reason, error.reason)

    def test_digest_mismatch_is_not_reported_as_unsupported_program(self):
        with patch.object(core, "_require_source_contract"), patch.object(core, "_validated_public_upstream"), \
             patch.object(text_conversion.upstream_contracts, "build_source_neutral_ir", return_value={"input": {"sha256": "0" * 64}}):
            with self.assertRaisesRegex(ValueError, "digest differs"):
                text_conversion._fx5_document(Path("synthetic.gx3"), b"different", None)
