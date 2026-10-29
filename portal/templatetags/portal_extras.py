from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def page_url(context, page_number):
    """Current URL's query string with only ``page`` changed.

    Works on every Django version (the built-in ``{% querystring %}`` tag
    only exists from Django 5.1).
    """
    request = context.get("request")
    params = request.GET.copy() if request is not None else None
    if params is None:
        return "?page={}".format(page_number)
    params["page"] = page_number
    return "?" + params.urlencode()
