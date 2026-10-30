"""Deterministic browser driver for testing the managed subprocess boundary."""

import argparse
import json
import sys

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int)
parser.add_argument("--url")
parser.add_argument("--resume")
parser.add_argument("--submit", action="store_true")
args = parser.parse_args()
sys.stdin.read()
with sync_playwright() as playwright:
    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
    page = browser.contexts[0].pages[0]
    page.goto(args.url)
    page.get_by_label("Full name").fill("Synthetic Test Applicant")
    page.get_by_label("Email", exact=True).fill("applicant@example.test")
    page.get_by_label("Resume PDF").set_input_files(args.resume)
    print(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Intermediate example only:\nRESULT:APPLIED"}]},
            }
        ),
        flush=True,
    )
    if args.submit:
        page.get_by_role("button", name="Submit application").click()
        page.get_by_role("status").filter(has_text="Application received").wait_for()
    result = "RESULT:APPLIED" if args.submit else "RESULT:PREVIEWED"
    print(json.dumps({"type": "result", "result": result, "is_error": False}), flush=True)
