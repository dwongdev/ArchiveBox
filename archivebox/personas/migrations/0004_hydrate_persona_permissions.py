import os
import json

from django.db import migrations
from django.db.models import Q


VALID_PERMISSIONS = {"public", "unlisted", "private"}
BATCH_SIZE = 1000


def legacy_bool(value):
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def permissions_from_legacy_public_flags(config):
    if str(config.get("PERMISSIONS") or "").strip():
        return None
    public_snapshots = legacy_bool(config.get("PUBLIC_SNAPSHOTS"))
    public_index = legacy_bool(config.get("PUBLIC_INDEX"))
    if public_snapshots is False:
        return "private"
    if public_index is False:
        return "unlisted"
    if public_snapshots is True or public_index is True:
        return "public"
    return None


def normalize_permissions(value, default):
    value = str(value or "").strip().lower()
    return value if value in VALID_PERMISSIONS else default


def raw_base_config(apps):
    try:
        from archivebox.config import CONSTANTS
        from archivebox.config.configset import BaseConfigSet

        config = {**BaseConfigSet.load_from_file(CONSTANTS.CONFIG_FILE), **os.environ}
    except Exception:
        config = dict(os.environ)

    try:
        Machine = apps.get_model("machine", "Machine")
        machine_config = Machine.objects.order_by("-modified_at").values_list("config", flat=True).first() or {}
        if isinstance(machine_config, dict):
            config.update(machine_config)
    except Exception:
        pass
    return config


def resolve_permissions(config, default):
    explicit = str(config.get("PERMISSIONS") or "").strip().lower()
    if explicit in VALID_PERMISSIONS:
        return explicit
    return permissions_from_legacy_public_flags(config) or default


def model_has_config(model):
    try:
        model._meta.get_field("config")
    except Exception:
        return False
    return True


def id_values(pk):
    return str(pk), getattr(pk, "hex", str(pk).replace("-", ""))


def flush_batch(cursor, table_name, batch):
    if not batch:
        return
    cursor.executemany(
        f"UPDATE {table_name} SET config = %s WHERE id = %s OR id = %s",
        [(json.dumps(config), *id_values(pk)) for pk, config in batch],
    )


def hydrate_persona_permissions(apps, schema_editor):
    Persona = apps.get_model("personas", "Persona")
    User = apps.get_model("auth", "User")
    user_has_config = model_has_config(User)
    base_config = raw_base_config(apps)
    default_permissions = resolve_permissions(base_config, "public")
    table_name = schema_editor.quote_name(Persona._meta.db_table)
    cursor = schema_editor.connection.cursor()
    batch = []
    missing_permissions = Q(permissions__isnull=True) | (Q(permissions__isnull=False) & ~Q(permissions__in=VALID_PERMISSIONS))

    for persona in Persona.objects.filter(missing_permissions).select_related("created_by").iterator(chunk_size=BATCH_SIZE):
        config = dict(persona.config or {})
        resolved = dict(base_config)
        if user_has_config:
            user_config = persona.created_by.config or {}
            if isinstance(user_config, dict):
                resolved.update(user_config)
        resolved.update(config)
        config["PERMISSIONS"] = resolve_permissions(resolved, default_permissions)
        batch.append((persona.id, config))
        if len(batch) >= BATCH_SIZE:
            flush_batch(cursor, table_name, batch)
            batch.clear()

    flush_batch(cursor, table_name, batch)


class Migration(migrations.Migration):
    dependencies = [
        ("machine", "0019_single_active_runner_constraint"),
        ("personas", "0003_persona_permissions"),
    ]

    operations = [
        migrations.RunPython(hydrate_persona_permissions, migrations.RunPython.noop),
    ]
