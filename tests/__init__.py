"""Test package for the Comix uploader.

Run everything from the project root:

    .venv\\Scripts\\python.exe -m unittest discover tests

These tests deliberately need no browser, no network and no Playwright:
core.py is stdlib-only, and parallel.py's browser plumbing is stubbed out.
"""
