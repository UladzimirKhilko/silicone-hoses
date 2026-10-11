from .pricing import can_see_costs


def costs(request):
    """В шаблонах: can_see_costs — показывать ли учётные цены и прибыль (только руководителю)."""
    return {"can_see_costs": can_see_costs(request.user) if hasattr(request, "user") else False}
