from django.shortcuts import render
from django.views.decorators.http import require_safe

from core.models import Fight, Person


@require_safe
def simulate(request):
    return render(request, "core/simulate.html", {
        "people": Person.objects.filter(rank__isnull=False).order_by("id"),
        "fights": Fight.objects.select_related("blue", "red").prefetch_related("events").order_by("id"),
    })
