"""Exercise application widgets against local HTML, with no accounts or sends."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "lib"))
from herald import ats


class Widgets(unittest.TestCase):
    def setUp(self):
        try:
            from playwright.sync_api import sync_playwright
            self.pw = sync_playwright().start()
            self.browser = self.pw.chromium.launch(headless=True)
        except Exception as exc:
            if hasattr(self, "pw"):
                self.pw.stop()
            self.skipTest(str(exc).splitlines()[0])
        self.page = self.browser.new_page()

    def tearDown(self):
        self.browser.close()
        self.pw.stop()

    def test_ashby_no_is_distinct_from_unanswered(self):
        self.page.set_content('''<div class="ashby-application-form-field-entry">
          <label class="required">Enrolled?</label>
          <div class="ashby-application-form-input-yesno">
          <button data-option="yes" aria-pressed="false">Yes</button>
          <button data-option="no" aria-pressed="true">No</button><input type="checkbox">
          </div></div>''')
        fields = self.page.evaluate(ats.SCAN_JS)
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["kind"], "buttonradio")
        self.assertTrue(fields[0]["required"])
        self.assertTrue(fields[0]["options"][1]["checked"])

    def test_workday_repeated_fields_keep_their_own_values(self):
        self.page.set_content('''<div data-automation-id="formField-jobTitle"><label>Title</label><input value="First"></div>
          <div data-automation-id="formField-jobTitle"><label>Title</label><input value="Second"></div>''')
        adapter = ats.Workday(self.page, {}, None, {}, resume="")
        fields = adapter.scan()
        adapter.set(fields[1], "Changed")
        self.assertEqual(self.page.locator("input").nth(0).input_value(), "First")
        self.assertEqual(self.page.locator("input").nth(1).input_value(), "Changed")

    def test_search_page_is_never_ready_for_submission(self):
        self.page.set_content('<label for="q">Search jobs</label><input id="q">')
        result = ats.Generic(self.page, {}, None, {}, resume="").fill()
        self.assertFalse(result["done"])


if __name__ == "__main__":
    unittest.main()
