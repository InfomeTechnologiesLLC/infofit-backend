INFOME FIT update - 29 Oct 2026 (replaces infome-fit-hotfix-django32.zip)

Copy these files over the same paths in infofit-backend, then restart the site
and the Celery worker. No migration needed. Works on Django 3.2 / Python 3.6.

Fixes for the live server (Django 3.2 / Python 3.6):
  portal/templatetags/__init__.py, portal_extras.py   new page-links tag
  portal/templates/portal/pagination.html              uses it instead of {% querystring %}
  challenges/scoring.py                                no dataclasses (Python 3.7+)
  fitness/views.py, portal/views.py, assistant/services.py   no date.fromisoformat (Python 3.7+)

Edit / deactivate / delete employees:
  portal/views.py, portal/urls.py                      new delete + deactivate actions
  portal/templates/portal/employee_delete.html         new confirmation page
  portal/templates/portal/employee_detail.html         Edit / Deactivate / Delete buttons
  portal/templates/portal/employees.html               edit + delete icons on each row
  portal/tests/test_portal.py                          tests
