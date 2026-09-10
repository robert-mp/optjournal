"""Platform-specific browser discovery stays useful outside the developer's Mac."""

from __future__ import annotations

from optjournal.browser import _windows_install_paths


def test_windows_browser_locations_follow_the_environment():
    paths = {
        str(path).replace("\\", "/")
        for path in _windows_install_paths({
            "PROGRAMFILES": "C:/Program Files",
            "PROGRAMFILES(X86)": "C:/Program Files (x86)",
            "LOCALAPPDATA": "C:/Users/Ada/AppData/Local",
        })
    }

    assert "C:/Program Files/Google/Chrome/Application/chrome.exe" in paths
    assert "C:/Program Files/Microsoft/Edge/Application/msedge.exe" in paths
    assert "C:/Users/Ada/AppData/Local/Chromium/Application/chrome.exe" in paths
