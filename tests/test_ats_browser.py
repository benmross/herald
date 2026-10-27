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

    def test_oracle_required_pills_are_unanswered_until_selected(self):
        self.page.set_content('''<div class="input-row">
          <label class="input-row__label--required">Authorized?</label>
          <ul role="radiogroup" aria-label="Authorized?">
          <li><button role="radio" aria-checked="false"
            onclick="this.setAttribute('aria-checked','true')">Yes</button></li>
          <li><button role="radio" aria-checked="false">No</button></li>
          </ul></div>''')
        adapter = ats.Generic(self.page, {}, None, {}, resume="")
        field = adapter.scan()[0]
        self.assertTrue(field["required"])
        self.assertFalse(any(o["checked"] for o in field["options"]))
        self.assertTrue(adapter.set(field, "Yes"))
        self.assertTrue(adapter.scan()[0]["options"][0]["checked"])

    def test_combobox_label_is_not_a_selected_value(self):
        self.page.set_content('''<div><label for="country">Country *</label>
          <input id="country" role="combobox" required></div>''')
        field = self.page.evaluate(ats.SCAN_JS)[0]
        self.assertEqual(field["value"], "")

    def test_hidden_radio_uses_its_visible_label(self):
        self.page.set_content('''<fieldset><legend>Eligible?</legend>
          <input type="radio" id="yes" name="eligible" style="display:none">
          <label for="yes">Yes</label></fieldset>''')
        adapter = ats.Generic(self.page, {}, None, {}, resume="")
        self.assertTrue(adapter.set(adapter.scan()[0], "Yes"))
        self.assertTrue(self.page.locator("#yes").is_checked())


if __name__ == "__main__":
    unittest.main()
