"""Synthetic reader failures and offscreen UI presentation; no project fixtures."""
from concurrent.futures import Future
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QApplication, QPushButton

from app.qt_app import Workspace
from app import qt_workflow as workflow
from app.workflow_service import WorkflowDiagnostic
from app.workflow_state import SourceIdentity


class WorkerFailureTests(unittest.TestCase):
    def setUp(self):
        self.identity = SourceIdentity.from_bytes("synthetic.gx3", b"synthetic")
        self.finding = WorkflowDiagnostic("SYNTHETIC_DECODE_FAILED", "error", "source",
                                          pou="Example", locator="record 7", reason="Synthetic reason",
                                          action="Inspect synthetic input", detail='<raw>\nopaque record')
        self.analysis = SimpleNamespace(source_identity=None, diagnostics=(self.finding,))
        self.connection = Mock()

    def run_failure(self, command="prepare", current=None):
        body = {"path": "synthetic.gx3", "command": command}
        with patch.object(workflow.core, "_reject_link_like_ancestors", side_effect=lambda path, _: path), \
                patch.object(workflow.SourceIdentity, "from_file", side_effect=[self.identity, current or self.identity]), \
                patch.object(workflow, "request_from", return_value=(SimpleNamespace(settings=None), None)), \
                patch.object(workflow.service, "analyze", return_value=self.analysis), \
                patch.object(workflow.service, "plan") as plan, \
                patch.object(workflow.service, "publish") as publish:
            workflow.qt_worker(self.connection, threading.Event(), body)
        plan.assert_not_called()
        publish.assert_not_called()
        self.connection.close.assert_called_once()
        return self.connection.send.call_args_list[-1].args[0]

    def test_missing_identity_preserves_all_reader_findings_and_blocks_plan(self):
        event = self.run_failure()
        self.assertEqual(event["phase"], "FAILED")
        self.assertEqual(event["error"]["code"], "SOURCE_ANALYSIS_FAILED")
        self.assertEqual(event["error"]["diagnostics"][0], workflow.asdict(self.finding))
        self.assertNotIn("result", event)
        self.assertNotIn("새 폴더", event["label"])
        self.assertNotIn("남은 파일", event["label"])

    def test_changed_bytes_are_distinct_from_decode_failure_and_keep_findings(self):
        event = self.run_failure(current=SourceIdentity.from_bytes("synthetic.gx3", b"changed"))
        self.assertEqual(event["error"]["code"], "SOURCE_CHANGED")
        self.assertEqual(event["error"]["diagnostics"][0]["detail"], self.finding.detail)

    def test_nonmatching_reader_identity_with_unchanged_bytes_is_not_source_changed(self):
        self.analysis.source_identity = SourceIdentity.from_bytes("synthetic.gx3", b"other")
        self.assertEqual(self.run_failure()["error"]["code"], "SOURCE_IDENTITY_MISMATCH")

    def test_recheck_failure_keeps_original_reader_findings(self):
        event = self.run_failure(current=OSError("synthetic recheck failure"))
        self.assertEqual(event["error"]["code"], "SOURCE_RECHECK_FAILED")
        self.assertIn("synthetic recheck failure", event["error"]["message"])
        self.assertEqual(event["error"]["diagnostics"][0]["reason"], self.finding.reason)

    def test_publication_failure_has_generation_guidance(self):
        event = self.run_failure(command="publish")
        self.assertIn("파일 생성", event["label"])
        self.assertIn("복구 상세", event["label"])

    def test_cancel_is_not_failed(self):
        with patch.object(workflow.core, "_reject_link_like_ancestors",
                          side_effect=workflow.core.ConversionCancelled("synthetic cancel")):
            workflow.qt_worker(self.connection, threading.Event(), {"path": "synthetic.gx3"})
        self.assertEqual(self.connection.send.call_args_list[-1].args[0]["phase"], "CANCELLED")

    def test_publish_partial_files_and_recovery_fields_are_retained(self):
        failure = RuntimeError("synthetic publication failure")
        failure.partial_paths = (workflow.Path("synthetic-result/partial.csv"),)
        failure.output_dir = workflow.Path("synthetic-result")
        self.analysis.source_identity = self.identity
        self.analysis.programs, self.analysis.raw_analysis, self.analysis.diagnostics = (), None, ()
        result = {"output_parent": "synthetic-parent", "binding": "binding", "planned_files": ["partial.csv", "missing.csv"],
                  "file_generation": "NOT_GENERATED"}
        body = {"path": "synthetic.gx3", "command": "publish", "mode": "convert",
                "execution_request": workflow.EXPLICIT_EXECUTION_REQUEST, "expected_binding": "binding"}
        with patch.object(workflow.core, "_reject_link_like_ancestors", side_effect=lambda path, _: path), \
                patch.object(workflow.SourceIdentity, "from_file", return_value=self.identity), \
                patch.object(workflow, "request_from", return_value=(SimpleNamespace(settings=None, direction="Q_TO_FX5"), None)), \
                patch.object(workflow.service, "analyze", return_value=self.analysis), \
                patch.object(workflow.service, "plan", return_value=SimpleNamespace(publishable=True)), \
                patch.object(workflow, "with_actual_cpu_report", side_effect=lambda plan, _: plan), \
                patch.object(workflow, "display_plan", return_value=result), \
                patch.object(workflow.Path, "is_dir", return_value=True), \
                patch.object(workflow.service, "publish", side_effect=failure):
            workflow.qt_worker(self.connection, threading.Event(), body)
        event = self.connection.send.call_args_list[-1].args[0]
        self.assertEqual(event["phase"], "FAILED")
        self.assertEqual(event["result"]["file_generation"], "FAILED")
        self.assertEqual(event["result"]["remaining_files"], [str(failure.partial_paths[0])])
        self.assertEqual(event["result"]["preserved_folder"], str(failure.output_dir))
        self.assertEqual(event["result"]["not_generated"], ["missing.csv"])


class FailurePresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        runtime = SimpleNamespace(args=SimpleNamespace(source="", auto_close=0), framework="SYNTHETIC",
                                  session=Mock(), closed=True, record=Mock(), gui_tick=Mock(),
                                  progress_record=Mock(), result_record=Mock())
        with patch.object(Workspace, "ready"):
            self.window = Workspace(runtime)
            self.window.timer.stop()
            self.window.show()
            self.app.processEvents()
        self.window.resize(1024, 768)
        self.app.processEvents()
        with QSignalBlocker(self.window.path):
            self.window.path.setText("synthetic.gx3")
        self.window.generation, self.window.request_id = 3, "current"
        self.window.snapshot_executor.submit = Mock(return_value=Future())

    def tearDown(self):
        self.window.error_dialog.close()
        self.window.close()
        self.app.processEvents()

    def feed(self, phase="FAILED", *, version=1, generation=3, request_id="current"):
        state = {"generation": generation, "request_id": request_id, "version": version,
                 "busy": False, "result": None,
                 "events": [{"phase": phase, "label": "분석에 실패했습니다. 오류 상세를 확인하세요.",
                             "error": {"code": "SYNTHETIC_ERROR", "message": "한글 <raw>\n" + "long detail " * 300,
                                       "diagnostics": [{"reason": "reason", "action": "action", "detail": "detail"}]}}]}
        future = Future()
        future.set_result(state)
        self.window.snapshot_future = future
        self.window.poll()
        self.app.processEvents()

    def test_one_popup_per_request_plain_text_and_reopen(self):
        with patch.object(self.window.error_dialog, "open", wraps=self.window.error_dialog.open) as opened:
            self.feed()
            self.assertEqual(opened.call_count, 1)
            self.assertTrue(self.window.error_dialog.isVisible())
            self.assertEqual(self.window.error_text.toPlainText(), self.window.operation_error_detail)
            self.assertIn("SYNTHETIC_ERROR", self.window.error_text.toPlainText())
            self.assertIn("<raw>", self.window.error_text.toPlainText())
            self.assertIn("한글 <raw>\nlong detail", self.window.error_text.toPlainText())
            for field in ("원인: reason", "다음 조치: action", "상세 원문:\ndetail"):
                self.assertIn(field, self.window.error_text.toPlainText())
            self.assertLess(self.window.error_text.toPlainText().index("다음 조치: action"),
                            self.window.error_text.toPlainText().index("전체 오류 데이터 (JSON):"))
            self.assertTrue(self.window.error_text.isReadOnly())
            self.window.error_dialog.accept()
            self.feed(version=2)
            self.assertEqual(opened.call_count, 1)
            self.assertFalse(self.window.error_dialog.isVisible())
            self.window.error_details.click()
            self.assertEqual(opened.call_count, 2)
            copy_button = next(button for button in self.window.error_dialog.findChildren(QPushButton)
                               if button.text() == "전체 복사")
            copy_button.click()
            self.assertEqual(self.app.clipboard().text(), self.window.operation_error_detail)

    def test_failed_footer_has_one_retry_and_separate_visible_status(self):
        self.feed()
        self.assertTrue(self.window.retry.isHidden())
        self.assertEqual(self.window.generate.text(), "분석 다시 시도")
        self.assertTrue(self.window.generate.isEnabled())
        self.assertTrue(self.window.status.wordWrap())
        self.assertLessEqual(self.window.status.geometry().bottom(), self.window.generate.geometry().top())
        self.assertLess(self.window.generate.mapTo(self.window, self.window.generate.rect().bottomRight()).y(),
                        self.window.height())

    def test_old_request_and_cancel_do_not_open_error_popup(self):
        with patch.object(self.window.error_dialog, "open") as opened:
            self.feed(generation=2, request_id="old")
            self.feed(phase="CANCELLED")
            opened.assert_not_called()
        self.assertTrue(self.window.error_details.isHidden())

    def test_new_request_clears_details_and_allows_one_new_popup(self):
        self.feed()
        self.window.identity()
        self.assertIsNone(self.window.operation_error)
        self.assertFalse(self.window.operation_error_detail)
        self.assertFalse(self.window.operation_error_message)
        self.assertTrue(self.window.error_details.isHidden())
        self.assertFalse(self.window.error_dialog.isVisible())
        self.assertFalse(self.window.error_text.toPlainText())
        self.feed(generation=self.window.generation, request_id=self.window.request_id)
        self.assertTrue(self.window.error_dialog.isVisible())

    def test_stale_snapshot_exception_does_not_report_against_current_request(self):
        future = Future()
        future.set_exception(OSError("old synthetic snapshot"))
        self.window.snapshot_request = (2, "old")
        self.window.snapshot_future = future
        with patch.object(self.window.error_dialog, "open") as opened:
            self.window.poll()
            opened.assert_not_called()
        self.assertIsNone(self.window.operation_error)

    def test_current_snapshot_exception_opens_full_error_details(self):
        future = Future()
        future.set_exception(OSError("current synthetic snapshot\noriginal detail"))
        self.window.snapshot_request = (3, "current")
        self.window.snapshot_future = future
        self.window.poll()
        self.assertTrue(self.window.error_dialog.isVisible())
        self.assertIn("current synthetic snapshot\noriginal detail", self.window.error_text.toPlainText())
        self.assertIn("current synthetic snapshot", self.window.generation_notice.text())
        self.assertIn("오류 상세를 확인한 뒤 분석 다시 시도하세요.", self.window.generation_notice.text())

    def test_banner_keeps_bounded_original_and_next_action(self):
        self.feed()
        banner = self.window.generation_notice.text()
        self.assertIn("분석에 실패했습니다. 오류 상세를 확인하세요.", banner)
        self.assertIn("오류 원문: 한글 <raw> long detail", banner)
        self.assertIn("오류 상세를 확인한 뒤 분석 다시 시도하세요.", banner)
        self.assertNotIn("long detail " * 300, banner)
        self.assertLess(len(banner), 400)
        self.assertIn("한글 <raw>\n" + "long detail " * 300, self.window.operation_error_detail)

    def test_short_original_stays_in_the_banner_and_cancel_drops_it(self):
        self.window.set_operation_error("분석 실패", {"code": "ZIP", "message": "File is not a zip file"})
        self.window.refresh_primary()
        banner = self.window.generation_notice.text()
        self.assertIn("File is not a zip file", banner)
        self.assertIn("오류 상세를 확인한 뒤 분석 다시 시도하세요.", banner)
        self.assertEqual("분석 다시 시도", self.window.generate.text())
        self.assertIn("File is not a zip file", self.window.operation_error_detail)
        state = {"generation": 3, "request_id": "current", "version": 4, "busy": False, "result": None,
                 "events": [{"phase": "CANCELLED", "label": "취소되었습니다. 입력은 유지됩니다."}]}
        future = Future()
        future.set_result(state)
        self.window.snapshot_future = future
        self.window.version = 0
        self.window.poll()
        self.app.processEvents()
        self.assertIn("취소되었습니다", self.window.generation_notice.text())
        self.assertNotIn("File is not a zip file", self.window.generation_notice.text())
        self.assertFalse(self.window.operation_error_message)


if __name__ == "__main__":
    unittest.main()
