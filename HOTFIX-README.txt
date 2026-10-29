INFOME FIT hotfix - portal pages on the live server (Django 3.2 / Python 3.6)

Copy these 7 files over the same paths in infofit-backend, then restart the site
(and the Celery worker). No migration needed.

  portal/templatetags/__init__.py        new
  portal/templatetags/portal_extras.py   new - page links tag that works on Django 3.2
  portal/templates/portal/pagination.html  uses the new tag instead of {% querystring %}
  challenges/scoring.py                  no dataclasses (Python 3.7+)
  fitness/views.py, portal/views.py, assistant/services.py
                                         no date.fromisoformat (Python 3.7+)
