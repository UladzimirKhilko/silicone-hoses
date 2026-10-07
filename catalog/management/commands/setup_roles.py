"""Создаёт группы «Руководитель» и «Менеджер» (docs/13-interview.md)."""

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

ROLES = ["Руководитель", "Менеджер"]


class Command(BaseCommand):
    help = "Создать роли пользователей"

    def handle(self, *args, **opts):
        for name in ROLES:
            Group.objects.get_or_create(name=name)
        self.stdout.write(self.style.SUCCESS("Роли: " + ", ".join(ROLES)))
