from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0044_alter_archiveresult_status_alter_snapshot_status"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="archiveresult",
            constraint=models.UniqueConstraint(
                fields=("snapshot", "plugin", "hook_name"),
                name="unique_archiveresult_per_snapshot_hook",
            ),
        ),
    ]
