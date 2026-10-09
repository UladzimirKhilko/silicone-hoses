"""Полные наименования заказчиков для паспорта (заполняются, только если пусто)."""

from django.db import migrations

FULL_NAMES = {
    "МТЗ": "ОАО «Минский тракторный завод»",
    "Гомсельмаш": "ОАО «Гомсельмаш»",
    "ММЗ": "ОАО «Минский моторный завод»",
    "БелАЗ": "ОАО «БЕЛАЗ» – управляющая компания холдинга «БЕЛАЗ-ХОЛДИНГ»",
}


def fill(apps, schema_editor):
    Customer = apps.get_model("catalog", "Customer")
    for name, full_name in FULL_NAMES.items():
        Customer.objects.filter(name=name, full_name="").update(full_name=full_name)


class Migration(migrations.Migration):
    dependencies = [("catalog", "0004_alter_product_options")]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
