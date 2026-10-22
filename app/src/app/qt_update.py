"""Network workers never access Qt; notices are owned by the application window."""
from queue import Empty, Queue
import threading

from PySide6.QtCore import QObject, QPoint, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from app.update_check import UpdateResult, check_for_update, release_page_url


class UpdateNotifier(QObject):
    def __init__(self, window, button, version, *, checker=check_for_update):
        super().__init__(window)
        self.window, self.button, self.version, self.checker = window, button, version, checker
        self.queue = Queue()
        self.active = self.closed = False
        self.pending = None
        self.dialog = None
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.startup = QTimer(self)
        self.startup.setSingleShot(True)
        self.startup.timeout.connect(self.check)
        button.clicked.connect(lambda: self.check(manual=True))
        button.setAccessibleName("업데이트 확인")
        button.setToolTip(f"현재 버전 {version}. GitHub의 최신 정식 버전을 확인합니다.")

    def start(self):
        self.startup.start(700)

    def stop(self):
        self.closed = True
        self.startup.stop()
        self.timer.stop()
        self.pending = None
        if self.dialog is not None:
            self.dialog.close()

    def check(self, manual=False):
        if self.active or self.closed:
            return
        self.startup.stop()
        self.active = True
        self.button.setEnabled(False)
        self.button.setText("확인 중...")
        # A detached worker only owns plain Python values, even after window close.
        checker, version, queue = self.checker, self.version, self.queue
        def work():
            try:
                result = checker(version)
            except Exception:
                result = UpdateResult("unavailable", reason="checker-error")
            queue.put((manual, result))
        threading.Thread(target=work, name="qfx-update-check", daemon=True).start()
        self.timer.start()

    def poll(self):
        if self.closed:
            return
        if self.pending is None:
            try:
                self.pending = self.queue.get_nowait()
            except Empty:
                return
        if (QApplication.activeModalWidget() is not None or self.window.busy
                or self.window.closing or self.window.isMinimized() or not self.window.isVisible()):
            return
        manual, result = self.pending
        self.pending = None
        self.timer.stop()
        self.active = False
        self.button.setEnabled(True)
        self.button.setText("업데이트 확인")
        if result.status == "available" and result.release is not None and self.trusted(result.release):
            self.show_notice("새 버전 알림", f"새로운 정식 버전을 사용할 수 있습니다.\n\n현재 버전: {self.version}\n최신 버전: {result.release.version}", result.release)
        elif manual:
            text = ("설치된 앱보다 새로운 정식 버전이 없습니다." if result.status == "current"
                    else "지금은 업데이트를 확인할 수 없습니다.\n잠시 후 다시 확인해 주세요. 앱은 계속 사용할 수 있습니다.")
            self.show_notice("업데이트 확인", text)

    @staticmethod
    def trusted(release):
        try:
            return release.page_url == release_page_url(release.tag)
        except (AttributeError, ValueError):
            return False

    def show_notice(self, title, text, release=None):
        if self.dialog is not None:
            self.dialog.close()
        dialog = QDialog(self.window)
        self.dialog = dialog
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(lambda: self._finished(dialog))
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(22, 20, 22, 20)
        message = QLabel(text)
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setWordWrap(True)
        layout.addWidget(message)
        row = QHBoxLayout()
        if release is not None:
            open_button = QPushButton("릴리스 페이지 열기")
            open_button.clicked.connect(lambda: self.open_release(release, message, dialog))
            row.addWidget(open_button)
        row.addStretch()
        later = QPushButton("나중에" if release is not None else "확인")
        later.clicked.connect(dialog.reject)
        row.addWidget(later)
        layout.addLayout(row)
        dialog.resize(min(430, self.window.width() - 24), dialog.sizeHint().height())
        center = self.window.mapToGlobal(self.window.rect().center())
        point = center - QPoint(dialog.width() // 2, dialog.height() // 2)
        available = self.window.screen().availableGeometry()
        point.setX(max(available.left(), min(point.x(), available.right() - dialog.width())))
        point.setY(max(available.top(), min(point.y(), available.bottom() - dialog.height())))
        dialog.move(point)
        dialog.show()
        later.setFocus()

    def _finished(self, dialog):
        if self.dialog is dialog:
            self.dialog = None

    def open_release(self, release, message, dialog):
        if not self.trusted(release):
            return
        if QDesktopServices.openUrl(QUrl(release.page_url)):
            dialog.accept()
        else:
            message.setText("릴리스 페이지를 열지 못했습니다. 기본 브라우저를 확인해 주세요.")
