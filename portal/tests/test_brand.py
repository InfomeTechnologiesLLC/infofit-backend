from django.test import TestCase


class BrandLogoTests(TestCase):
    def test_login_page_shows_logo_and_favicon(self):
        html = self.client.get("/portal/login/").content.decode()
        self.assertIn('alt="INFOME FIT"', html)
        self.assertIn('rel="icon" type="image/png" href="data:image/png;base64,', html)
        self.assertIn('src="data:image/png;base64,', html)
