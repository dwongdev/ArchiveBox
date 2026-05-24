__package__ = "archivebox.core"

from typing import TYPE_CHECKING, Any

from django.contrib import admin
from django.db import DatabaseError, connection
from admin_data_views.admin import (
    admin_data_index_view as adv_admin_data_index_view,
    get_admin_data_urls as adv_get_admin_data_urls,
    get_app_list as adv_get_app_list,
)

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.template.response import TemplateResponse
    from django.urls import URLPattern, URLResolver

    from admin_data_views.typing import AppDict


class ArchiveBoxAdmin(admin.AdminSite):
    site_header = "ArchiveBox"
    index_title = "Admin Views"
    site_title = "Admin"
    namespace = "admin"

    def get_app_list(self, request: "HttpRequest", app_label: str | None = None) -> list["AppDict"]:
        if app_label is None:
            return adv_get_app_list(self, request)
        return adv_get_app_list(self, request, app_label)

    def admin_data_index_view(self, request: "HttpRequest", **kwargs: Any) -> "TemplateResponse":
        return adv_admin_data_index_view(self, request, **kwargs)

    def index(self, request: "HttpRequest", extra_context: dict[str, Any] | None = None) -> "TemplateResponse":
        response = super().index(request, extra_context)
        if connection.vendor != "sqlite":
            return response

        models_by_table: dict[str, list[dict[str, Any]]] = {}
        for app in response.context_data.get("app_list", []):
            for model in app.get("models", []):
                model_class = model.get("model")
                if not model_class or not model.get("perms", {}).get("view"):
                    continue
                models_by_table.setdefault(model_class._meta.db_table, []).append(model)

        if not models_by_table:
            return response

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT tbl, stat FROM sqlite_stat1")
                for table, stat in cursor.fetchall():
                    models = models_by_table.get(table)
                    if not models:
                        continue
                    try:
                        count = int(str(stat).split()[0])
                    except (IndexError, TypeError, ValueError):
                        continue
                    if count >= 1_000_000_000:
                        count_label = f"{count / 1_000_000_000:.1f}B"
                    elif count >= 1_000_000:
                        count_label = f"{count / 1_000_000:.1f}M"
                    elif count >= 1_000:
                        count_label = f"{count / 1_000:.1f}K"
                    else:
                        count_label = f"{count:,}"
                    count_label = count_label.replace(".0", "")
                    for model in models:
                        model["object_count"] = count
                        model["object_count_label"] = count_label
                        model["object_count_title"] = f"Approximate count from SQLite stats: {count:,}"
                    models_by_table.pop(table, None)
        except DatabaseError:
            pass
        return response

    def get_admin_data_urls(self) -> list["URLResolver | URLPattern"]:
        return adv_get_admin_data_urls(self)

    def get_urls(self) -> list["URLResolver | URLPattern"]:
        return self.get_admin_data_urls() + super().get_urls()


archivebox_admin = ArchiveBoxAdmin()
# Note: delete_selected is enabled per-model via actions = ['delete_selected'] in each ModelAdmin
# TODO: https://stackoverflow.com/questions/40760880/add-custom-button-to-django-admin-panel


############### Admin Data View sections are defined in settings.ADMIN_DATA_VIEWS #########


def register_admin_site():
    """Replace the default admin site with our custom ArchiveBox admin site."""
    from django.contrib import admin
    from django.contrib.admin import sites

    admin.site = archivebox_admin
    sites.site = archivebox_admin

    # Register admin views for each app
    # (Previously handled by ABX plugin system, now called directly)
    from archivebox.core.admin import register_admin as register_core_admin
    from archivebox.crawls.admin import register_admin as register_crawls_admin
    from archivebox.api.admin import register_admin as register_api_admin
    from archivebox.machine.admin import register_admin as register_machine_admin
    from archivebox.personas.admin import register_admin as register_personas_admin
    from archivebox.workers.admin import register_admin as register_workers_admin

    register_core_admin(archivebox_admin)
    register_crawls_admin(archivebox_admin)
    register_api_admin(archivebox_admin)
    register_machine_admin(archivebox_admin)
    register_personas_admin(archivebox_admin)
    register_workers_admin(archivebox_admin)

    return archivebox_admin
