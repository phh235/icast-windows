"""On-demand connection diagnostics."""
from codecs import getincrementaldecoder
from pathlib import Path

from PySide6.QtCore import QProcess
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout

from .control.usb import _python


def show_diagnostics(parent, receiver_summary):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Connection Diagnostics")
    dialog.resize(560, 380)
    layout = QVBoxLayout(dialog)
    output = QPlainTextEdit(receiver_summary + "\n\nChecking optional USB control...\n", dialog)
    output.setReadOnly(True)
    layout.addWidget(output)
    buttons = QDialogButtonBox(QDialogButtonBox.Close, dialog)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    process = QProcess(dialog)
    process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
    process.setProcessChannelMode(QProcess.MergedChannels)
    decoder = getincrementaldecoder("utf-8")("replace")

    def read_output(final=False):
        text = decoder.decode(bytes(process.readAllStandardOutput()), final=final)
        if text:
            output.moveCursor(QTextCursor.End)
            output.insertPlainText(text)

    process.readyReadStandardOutput.connect(read_output)
    process.finished.connect(lambda: read_output(final=True))
    process.errorOccurred.connect(lambda: output.appendPlainText(f"USB diagnostics error: {process.errorString()}"))
    process.start(_python(), ["-E", "-X", "utf8", "-m", "icast.control", "doctor"])
    try:
        dialog.exec()
    finally:
        if process.state() != QProcess.NotRunning:
            process.kill()
            process.waitForFinished(1000)
        dialog.deleteLater()
