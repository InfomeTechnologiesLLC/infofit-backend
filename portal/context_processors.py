"""Small counts shown as badges in the portal sidebar (only for signed-in staff on portal pages)."""
from community.models import CommunityPost, CommunityReport


def nav_counts(request):
    if not request.path.startswith("/portal/") or not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    if not request.user.is_staff:
        return {}
    return {"nav_counts": {
        "review": CommunityPost.objects.filter(status=CommunityPost.Status.REVIEW).count(),
        "reports": CommunityReport.objects.filter(status=CommunityReport.Status.OPEN).count(),
        "notifications_problem": _notifications_problem(),
    }}


def _notifications_problem():
    from .notification_health import has_problem
    try:
        return has_problem()
    except Exception:  # never break a page over the sidebar dot
        return False


_BRAND = None


def brand(request):
    """INFOME FIT logo images as data URIs, so they show even when the server doesn't serve static files."""
    global _BRAND
    if _BRAND is None:
        import base64
        from pathlib import Path
        folder = Path(__file__).resolve().parent / "static" / "portal" / "img"
        images = {}
        for key, name in (("logo", "logo.png"), ("mark", "mark-tile.png"), ("favicon", "favicon.png")):
            try:
                images[key] = "data:image/png;base64," + base64.b64encode((folder / name).read_bytes()).decode("ascii")
            except OSError:
                images[key] = ""
        _BRAND = images
    return {"brand": _BRAND}
