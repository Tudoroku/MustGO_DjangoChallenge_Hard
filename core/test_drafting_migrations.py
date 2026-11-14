from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class DraftMigrationTests(TransactionTestCase):
    def test_upgrade_preserves_existing_people_and_nullable_fields(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes("core")
        old_target = [("core", "0001_initial")]
        try:
            executor.migrate(old_target)
            old_apps = executor.loader.project_state(old_target).apps
            old_person = old_apps.get_model("core", "Person")
            old_person.objects.create(id=0, name="Original", age=21, key="keep-me")
            old_person.objects.create(id=99, name="Hellstorm Fireburn", age=24, key="fight-info")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
        apps = executor.loader.project_state(latest).apps
        person = apps.get_model("core", "Person")
        self.assertEqual(list(person.objects.order_by("id").values_list("id", "name", "age", "key")), [
            (0, "Original", 21, "keep-me"), (99, "Hellstorm Fireburn", 24, "fight-info"),
        ])
        self.assertEqual(list(person.objects.values_list("rank", "health", "draft_health", "draft_batch_id")),
                         [(None, None, None, None), (None, None, None, None)])
