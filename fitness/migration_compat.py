"""Add the Apple Health columns on Django 3.2 through 5.x, including MySQL 5.7."""
from django import VERSION
from django.db import migrations


def database_default(value):
    # Field.db_default was added in Django 5.0. Older runtimes still use the
    # ordinary Python default; AddFieldWithDatabaseDefault retains the SQL one
    # only on databases that support it.
    return {"db_default": value} if VERSION >= (5, 0) else {}


class AddFieldWithDatabaseDefault(migrations.AddField):
    """Retain a plain column's initial SQL default when the database supports it.

    MySQL 5.7 cannot retain JSON defaults. Django's normal add_field operation
    backfills the Python default there, without a permanent SQL default. That
    deployment requires a maintenance window with application writers stopped.
    """

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if VERSION >= (5, 0):
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        model = to_state.apps.get_model(app_label, self.model_name)
        if not self.allow_migrate_model(schema_editor.connection.alias, model):
            return
        field = model._meta.get_field(self.name)
        if (not self.preserve_default or not field.has_default() or field.null
                or field.is_relation or field.primary_key or field.unique or field.db_index):
            raise ValueError("This operation requires a plain non-null column with a default.")
        if schema_editor.skip_default(field):
            # In particular, the MySQL schema editor supplies a one-off UPDATE
            # for JSON defaults. Use that complete operation, not column_sql(),
            # so existing Android rows receive {} before new workers start.
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        definition, params = schema_editor.column_sql(model, field, include_default=True)
        db_params = field.db_parameters(connection=schema_editor.connection)
        if db_params["check"]:
            definition += " " + schema_editor.sql_check_constraint % db_params
        # SQLite doesn't accept bound parameters in an ALTER TABLE DEFAULT.
        if schema_editor.connection.vendor == "sqlite" and params:
            definition = definition % tuple(schema_editor.quote_value(value) for value in params)
            params = None
        schema_editor.execute(schema_editor.sql_create_column % {
            "table": schema_editor.quote_name(model._meta.db_table),
            "column": schema_editor.quote_name(field.column),
            "definition": definition,
        }, params)
