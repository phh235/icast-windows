# Repository Guidelines

## Project Structure

iCast mirrors iPhones on Windows 10 and 11 using Python 3.11+.

- `icast/` contains the AirPlay receiver, video and audio decoding, settings, and the PySide6 window. The app entry point is `icast/__main__.py` and the icon is `icast/icon.ico`.
- `icast/control/` contains optional USB control, the WebDriverAgent client, and IPA packaging. `icast/fairplay/` contains the FairPlay implementation.
- `tests/` contains unit tests and FairPlay vectors. `tools/` contains the iPhone simulator and FairPlay code generator.

## Build, Test, and Run

Run commands from the repository root in PowerShell. For `python` commands, use the installed environment at `%LOCALAPPDATA%\iCast\venv\Scripts\python.exe` or an equivalent development environment.

- `.\install.bat --mirror-only` installs mirroring and audio. `.\install.bat` also installs the USB control dependencies.
- `.\run.bat` launches iCast. `.\control.bat doctor` checks the optional USB setup.
- `python -m unittest discover -s tests -q` runs the unit suite. `python tests/test_fairplay.py` runs the FairPlay known-answer vectors.
- To simulate mirroring, run `python -m icast --local` and then `python tools/fake_iphone.py 127.0.0.1 7100` in a second terminal. The simulator needs NumPy.

## Coding Style

Use four spaces, `snake_case` for functions and variables, `PascalCase` for classes, and `UPPER_CASE` for constants. Follow nearby formatting and keep imports grouped by standard library, third-party, and local modules. Keep USB dependencies optional and use `pathlib` for filesystem paths. Run `git diff --check` before submitting.

## Testing Guidelines

Use `unittest` and `IsolatedAsyncioTestCase` for async tests. Name test modules `test_*.py` and methods `test_*`. Cover the affected behavior with focused regression tests. Use synthetic ZIPs and mock WDA servers for automated coverage, and report real-device checks separately.

## Commits and Pull Requests

Follow the existing `type(scope): summary` style, such as `fix(airplay): close idle streams`. Keep each commit focused. In a pull request, describe the behavior change and validation, link the relevant issue when available, and include screenshots for visible UI changes. Keep local settings, logs, and generated IPA files out of commits.
