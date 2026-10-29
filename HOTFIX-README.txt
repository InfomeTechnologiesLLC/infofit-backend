INFOME FIT update - 29 Oct 2026 (replaces the two earlier zips from today)

Copy these files over the same paths in infofit-backend, then restart the site
and the Celery worker. No migration needed. Works on Django 3.2 / Python 3.6.

Fixes for the live server (Django 3.2 / Python 3.6):
  portal/templatetags/__init__.py, portal_extras.py   new page-links tag
  portal/templates/portal/pagination.html              uses it instead of {% querystring %}
  challenges/scoring.py                                no dataclasses (Python 3.7+)
  fitness/views.py, portal/views.py, assistant/services.py   no date.fromisoformat (Python 3.7+)

Employees - edit, set password, deactivate, delete:
  portal/views.py, portal/urls.py, portal/forms.py     new actions
  portal/templates/portal/employee_delete.html         delete confirmation page
  portal/templates/portal/employee_detail.html         Edit / Set password / Deactivate / Delete buttons
  portal/templates/portal/employees.html               edit, password and delete icons on each row
  portal/tests/test_portal.py                          tests
