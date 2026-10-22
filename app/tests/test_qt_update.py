import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QPushButton, QWidget

from app.qt_update import UpdateNotifier
from app.update_check import UpdateResult, parse_release
from test_update_check import release_payload


class QtUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # Other UI suites may retain an owned modal. An unrelated modal must not
        # turn this test's deliberate deferred-notice behavior into a timeout.
        for widget in self.app.topLevelWidgets():
            if isinstance(widget, QDialog):
                widget.close()
        self.app.processEvents()
        self.window = QWidget()
        self.window.busy = self.window.closing = False
        self.window.resize(800, 550)
        self.window.move(120, 90)
        layout = QHBoxLayout(self.window)
        self.button = QPushButton("업데이트 확인")
        layout.addWidget(self.button)
        self.window.show()
        self.app.processEvents()
        self.release = parse_release(release_payload())
        self.notifier = UpdateNotifier(self.window, self.button, "2.2.63",
                                       checker=lambda _: UpdateResult("available", self.release))

    def tearDown(self):
        self.notifier.stop()
        self.window.close()
        self.app.processEvents()

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.notifier.active and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertFalse(self.notifier.active)

    def test_auto_new_version_opens_centered_owned_popup(self):
        self.notifier.start()
        self.notifier.startup.timeout.emit()
        self.wait_idle()
        dialog = self.notifier.dialog
        self.assertIsNotNone(dialog)
        self.assertEqual(self.window, dialog.parent())
        center = self.window.mapToGlobal(self.window.rect().center())
        actual = dialog.mapToGlobal(dialog.rect().center())
        self.assertLess((center - actual).manhattanLength(), 40)
        self.assertEqual("새 버전 알림", dialog.windowTitle())

    def test_manual_current_and_network_failure_show_feedback(self):
        for result in (UpdateResult("current", self.release), UpdateResult("unavailable")):
            self.notifier.checker = lambda _, result=result: result
            self.button.click()
            self.wait_idle()
            self.assertEqual("업데이트 확인", self.notifier.dialog.windowTitle())
            self.notifier.dialog.close()
            self.app.processEvents()

    def test_automatic_current_or_failure_is_silent(self):
        for result in (UpdateResult("current", self.release), UpdateResult("unavailable")):
            self.notifier.checker = lambda _, result=result: result
            self.notifier.check()
            self.wait_idle()
            self.assertIsNone(self.notifier.dialog)

    def test_busy_and_modal_defer_notice(self):
        self.window.busy = True
        self.notifier.active = True
        self.notifier.queue.put((False, UpdateResult("available", self.release)))
        self.notifier.poll()
        self.assertIsNone(self.notifier.dialog)
        self.window.busy = False
        modal = QDialog(self.window)
        modal.setModal(True)
        modal.show()
        self.app.processEvents()
        self.notifier.poll()
        self.assertIsNone(self.notifier.dialog)
        modal.close()
        self.app.processEvents()
        self.notifier.poll()
        self.assertIsNotNone(self.notifier.dialog)

    def test_duplicate_check_and_close_during_network_work(self):
        entered, finish = threading.Event(), threading.Event()
        calls = []
        def checker(version):
            calls.append(version)
            entered.set()
            finish.wait(2)
            return UpdateResult("available", self.release)
        self.notifier.checker = checker
        self.notifier.check()
        self.assertTrue(entered.wait(1))
        self.notifier.check(manual=True)
        self.assertEqual(["2.2.63"], calls)
        self.notifier.stop()
        finish.set()
        self.app.processEvents()
        self.notifier.poll()
        self.assertIsNone(self.notifier.dialog)

    def test_only_user_click_opens_trusted_page(self):
        with patch("app.qt_update.QDesktopServices.openUrl", return_value=True) as opened:
            self.notifier.check()
            self.wait_idle()
            opened.assert_not_called()
            button = next(b for b in self.notifier.dialog.findChildren(QPushButton) if b.text() == "릴리스 페이지 열기")
            button.click()
            self.assertEqual(self.release.page_url, opened.call_args.args[0].toString())


if __name__ == "__main__":
    unittest.main()
