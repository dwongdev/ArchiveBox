__package__ = "archivebox.misc"

from django.core.paginator import Paginator
from django.core.paginator import Page
from django.core.paginator import EmptyPage
from django.db import connection
from django.utils.functional import cached_property


class CountlessPage(Page):
    def __init__(self, object_list, number, paginator, has_next_page=False):
        self.has_next_page = has_next_page
        super().__init__(object_list, number, paginator)

    def has_next(self):
        return self.has_next_page


class CountlessPaginator(Paginator):
    has_exact_count = False

    @cached_property
    def count(self):
        return getattr(self, "_count_hint", 0)

    @cached_property
    def num_pages(self):
        return self.count

    def validate_number(self, number):
        try:
            number = int(number)
        except (TypeError, ValueError):
            raise EmptyPage("That page number is not an integer")
        if number < 1:
            raise EmptyPage("That page number is less than 1")
        return number

    def page(self, number):
        number = self.validate_number(number)
        bottom = (number - 1) * self.per_page
        page_objects = list(self.object_list[bottom : bottom + self.per_page + 1])
        has_next_page = len(page_objects) > self.per_page
        if has_next_page:
            page_objects = page_objects[: self.per_page]
        self._count_hint = bottom + len(page_objects) + (1 if has_next_page else 0)
        return CountlessPage(page_objects, number, self, has_next_page=has_next_page)


class AcceleratedPaginator(Paginator):
    """
    Accelerated paginator ignores DISTINCT when counting total number of rows.
    Speeds up SELECT Count(*) on Admin views by >20x.
    https://hakibenita.com/optimizing-the-django-admin-paginator
    """

    @cached_property
    def count(self):
        query = getattr(self.object_list, "query", None)
        if query is not None and (getattr(query, "distinct", False) or getattr(getattr(query, "where", None), "children", None)):
            # fallback to normal count method on filtered queryset
            return super().count

        model = getattr(self.object_list, "model", None)
        if model is None:
            return super().count

        # otherwise count total rows in a separate fast query
        if connection.vendor == "sqlite":
            table_name = model._meta.db_table
            with connection.cursor() as cursor:
                try:
                    cursor.execute("SELECT stat FROM sqlite_stat1 WHERE tbl = %s", [table_name])
                    stats = [int(str(row[0]).split()[0]) for row in cursor.fetchall() if row and row[0]]
                except Exception:
                    stats = []
            if stats:
                return max(stats)

        return model.objects.count()

        # Alternative approach for PostgreSQL: fallback count takes > 200ms
        # from django.db import connection, transaction, OperationalError
        # with transaction.atomic(), connection.cursor() as cursor:
        #     cursor.execute('SET LOCAL statement_timeout TO 200;')
        #     try:
        #         return super().count
        #     except OperationalError:
        #         return 9999999999999
