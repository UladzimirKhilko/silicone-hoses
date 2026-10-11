"""Создаёт группы «Руководитель» и «Менеджер» (docs/13-interview.md).

Менеджер работает со всем (справочник, приходы, отгрузки, списания, заявки), но не видит
учётных цен и прибыли. Руководитель видит всё — у группы право stock.view_costs.
Администратор (superuser) видит всё без групп.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

ROLES = ["Руководитель", "Менеджер"]
COSTS_ROLES = ["Руководитель"]


class Command(BaseCommand):
    help = "Создать роли пользователей"

    def handle(self, *args, **opts):
        view_costs = Permission.objects.get(content_type__app_label="stock", codename="view_costs")
        for name in ROLES:
            group, _ = Group.objects.get_or_create(name=name)
            if name in COSTS_ROLES:
                group.permissions.add(view_costs)
            else:
                group.permissions.remove(view_costs)
        self.stdout.write(self.style.SUCCESS("Роли: " + ", ".join(ROLES) + " (учётные цены и прибыль — " + ", ".join(COSTS_ROLES) + ")"))
