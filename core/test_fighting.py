from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.db import IntegrityError, connection, connections
from django.test import TestCase, TransactionTestCase

from core.fighting import replay_fight, save_fight_batch, validate_fights
from core.models import Fight, FightEvent, Person
from core.task_base import TransientNetworkError
from core.tasks import simulate_fight


def payload(fight_id=0, steps=None, **extra):
    return {"data": [{"fight_id": fight_id, "blue_soldier_id": 0, "red_soldier_id": 1,
                      "steps": steps if steps is not None else [
                          {"attacker": 1, "defender": 0, "damage": 8},
                          {"attacker": 0, "defender": 1, "damage": 5},
                      ]}], "next": None, **extra}


class FightReplayTests(TestCase):
    def setUp(self):
        for i in range(2):
            Person.objects.create(id=i, name=f"Soldier {i}", age=21, rank="General", health=100, draft_health=100)

    def test_order_health_and_idempotent_repeat(self):
        save_fight_batch(0, payload())
        self.assertTrue(replay_fight(0))
        self.assertFalse(replay_fight(0))
        save_fight_batch(0, payload())
        self.assertEqual(list(Person.objects.order_by("id").values_list("health", flat=True)), [92, 95])
        self.assertEqual(list(FightEvent.objects.values_list("position", "remaining_health")), [(0, 92), (1, 95)])
        self.assertEqual(Fight.objects.get(pk=0).status, "completed")
        response = self.client.get("/simulate")
        self.assertContains(response, "Fight 0")
        self.assertContains(response, "Attack history")
        self.assertContains(response, "completed")

    def test_identical_events_are_distinct_and_below_zero_is_exact(self):
        event = {"attacker": 1, "defender": 0, "damage": 60}
        save_fight_batch(0, payload(steps=[event, event]))
        replay_fight(0)
        self.assertEqual(Person.objects.get(pk=0).health, -20)
        self.assertEqual(list(FightEvent.objects.values_list("remaining_health", flat=True)), [40, -20])

    def test_invalid_late_fight_rolls_back_entire_import(self):
        data = payload()
        data["data"].append({"fight_id": 1, "blue_soldier_id": 0, "red_soldier_id": 1,
                             "steps": [{"attacker": 0, "defender": 9, "damage": 1}]})
        with self.assertRaises(ValueError):
            save_fight_batch(0, data)
        self.assertFalse(Fight.objects.exists())

    def test_rollback_then_retry_does_not_double_damage(self):
        save_fight_batch(0, payload())
        with connection.cursor() as cursor:
            cursor.execute("""CREATE TRIGGER fight_test_abort BEFORE UPDATE ON core_person
                              WHEN NEW.id=1 BEGIN SELECT RAISE(ABORT, 'fight rollback'); END""")
        try:
            with self.assertRaises(IntegrityError):
                replay_fight(0)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER fight_test_abort")
        self.assertEqual(Person.objects.get(pk=0).health, 100)
        self.assertFalse(FightEvent.objects.exclude(remaining_health=None).exists())
        replay_fight(0)
        self.assertEqual(Person.objects.get(pk=0).health, 92)

    def test_task_retries_transient_failure(self):
        save_fight_batch(0, payload())
        real = replay_fight
        calls = []

        def first_timeout(fight_id):
            calls.append(fight_id)
            if len(calls) == 1:
                raise TransientNetworkError("temporary")
            return real(fight_id)

        with patch("core.fighting.replay_fight", side_effect=first_timeout):
            result = simulate_fight.apply(args=[0]).get()
        self.assertTrue(result["applied"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(Person.objects.get(pk=0).health, 92)

    def test_navigation_supports_confirmed_shapes_and_rejects_conflicts(self):
        self.assertEqual(validate_fights({**payload(), "next": 7}), (7, ""))
        saved = payload()
        del saved["next"]
        saved["next_batch"] = 3
        self.assertEqual(validate_fights(saved), (3, ""))
        with self.assertRaises(ValueError):
            validate_fights({**payload(), "next": 1, "next_batch": 2})
        loop = save_fight_batch(0, {**payload(), "next": 0})
        self.assertIn("repeated", loop.navigation_issue)


class ConcurrentFightTests(TransactionTestCase):
    def test_overlapping_fights_and_duplicate_delivery_share_global_health(self):
        for i in range(2):
            Person.objects.create(id=i, name=f"Soldier {i}", age=21, rank="General", health=100)
        data = payload()
        data["data"].extend(payload(fight_id=1)["data"])
        save_fight_batch(0, data)

        def run(fight_id):
            try:
                return replay_fight(fight_id)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=4) as pool:
            applied = list(pool.map(run, [0, 1, 0, 1]))
        self.assertEqual(sum(applied), 2)
        self.assertEqual(list(Person.objects.order_by("id").values_list("health", flat=True)), [84, 90])
        self.assertEqual(FightEvent.objects.count(), 4)
        self.assertEqual(Fight.objects.filter(status="completed").count(), 2)
