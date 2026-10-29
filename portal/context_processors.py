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
    }}
