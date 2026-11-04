# Employee web app

Open **`https://<backend-host>/app/`** in Safari. Paste the employee's HealthySync
Auth token (`ahs_…`) at the sign-in screen. HR's **Manage iPhone sync** page now
includes a copyable app link and the link in its setup instructions.

The web app follows the Android design in `ui/theme/Color.kt`,
`InfomeHomeSections.kt`, `RankingsScreen.kt`, `ChallengeScreen.kt` and
`InfomeBottomNav.kt`: Infome red, light cards, step ring, ranking podium and
rounded bottom navigation.

## Screens

- **Home:** today's synced steps, daily goal, energy/distance/exercise, weekly
  step history, current challenges and a shortcut to rankings.
- **Challenges:** open and joined challenges, automatic progress, points and badges.
- **Rankings:** today/week/month steps, points, teams, personal position and name
  visibility. Refreshes every 30 seconds while this screen is visible.
- **Community:** paginated company/team feed, text and photo posts, hashtags,
  reactions, comments, reports, and removal of your own posts.
- **Profile:** employee/team, device and sync status, sign-out and setup help.

Safari's **Share → Add to Home Screen** gives a convenient app-style shortcut.
The web app needs a connection. HealthySync continues uploading health records;
refreshing a web page only reads records already received by the backend.

## Authentication and shared behavior

`/app/login/` exchanges a valid, active-device employee token for a Django browser
session. The token is submitted by CSRF-protected POST, never in a URL. Only the
connection ID and token digest are kept in the server-side session. No token is
stored in localStorage or sessionStorage or sent to JavaScript. Normal Django
session expiry applies (14 days by default).

Each page/API request checks that the employee and device connection are active
and the digest still matches. Token rotation/revocation, account deactivation,
or a source switch ends access on the next request. Sign-out ends browser access
without revoking HealthySync. Staff-role tokens are not accepted here.

The browser identity is separate from Django's staff login. Only the explicitly
listed `/app/api/` handlers accept it; all mutations enforce CSRF. Those handlers
reuse the Android API views, including challenge scoring, team visibility,
moderation, photo access and ranking privacy. Existing JWT and upload endpoints
retain their authentication. Responses containing employee data use `no-store`.

## Deploy and verify

Deploy the `employee_web/` directory together with the `config` and portal changes.
There is no additional database migration for this web app; the HealthySync
migration and normal Django sessions migration must already be applied.

For the existing Python 3.9/Django 4.1/MySQL 5.7 server, follow the maintenance-window
procedure in [`fitness/APPLE_HEALTH.md`](../fitness/APPLE_HEALTH.md). It uses the
existing `newenv`, a fitness-only migration target, and the production settings
with `"employee_web"` added to `INSTALLED_APPS`. This UI adds no Python packages.

The three CSS/JS files are served by an explicit `/app/assets/` allowlist, including
under Gunicorn with `DEBUG=False`. No static-file proxy, CDN, dependency or frontend
build is needed for this UI. The files also remain available to `collectstatic`.
Production uses the existing HTTPS, secure-cookie and trusted-proxy configuration.

```sh
# Development/test database only; production's Python has no SQLite module.
python manage.py test employee_web portal fitness challenges community accounts
python manage.py makemigrations --check --dry-run
```
