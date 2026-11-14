"""Persistent soldier health; SQLite IMMEDIATE serializes each complete replay.

Tasks execute on concurrent worker processes. SQLite admits one short writer
transaction at a time, so overlapping fights cannot lose updates. Commit order
defines cross-fight order. Every supplied attack is subtracted, including below
zero; no unconfirmed death, clamping or winner rules are added.
"""

from django.db import transaction
from django.utils import timezone

from core.drafting import validate_batch_id
from core.models import Fight, FightBatch, FightEvent, Person


def validate_fights(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("fight response requires a data array")
    if "status" in payload and (type(payload["status"]) is not int or payload["status"] != 200):
        raise ValueError("fight response status must be integer 200 when present")
    seen = set()
    for row in payload["data"]:
        if not isinstance(row, dict):
            raise ValueError("fight must be an object")
        validate_batch_id(row.get("fight_id"))
        if row["fight_id"] in seen:
            raise ValueError("duplicate fight_id in batch")
        seen.add(row["fight_id"])
        participants = [row.get("blue_soldier_id"), row.get("red_soldier_id")]
        if any(type(i) is not int or not 0 <= i <= 99 for i in participants) or participants[0] == participants[1]:
            raise ValueError("fight requires two distinct valid soldier IDs")
        if not isinstance(row.get("steps"), list):
            raise ValueError("fight steps must be an array")
        for event in row["steps"]:
            if (not isinstance(event, dict)
                    or any(type(event.get(k)) is not int or event[k] not in participants for k in ("attacker", "defender"))
                    or event["attacker"] == event["defender"]
                    or type(event.get("damage")) is not int or not 0 <= event["damage"] <= 2147483647):
                raise ValueError("invalid attack step or participant/damage")
    # Organizer says 'next'; the actual saved fight-0.json uses 'next_batch'.
    if "next" in payload and "next_batch" in payload and payload["next"] != payload["next_batch"]:
        raise ValueError("conflicting next and next_batch pointers")
    pointer_field = "next" if "next" in payload else "next_batch"
    pointer = payload.get(pointer_field)
    if pointer_field not in payload:
        return None, "missing navigation pointer; chain stopped, completion unverified"
    if pointer is None:
        return None, "null navigation pointer; chain stopped"
    try:
        return validate_batch_id(pointer), ""
    except ValueError:
        return None, "invalid navigation pointer; chain stopped"


def save_fight_batch(batch_id, payload):
    validate_batch_id(batch_id)
    next_id, issue = validate_fights(payload)
    with transaction.atomic():
        saved = FightBatch.objects.filter(pk=batch_id).first()
        if saved and saved.payload != payload:
            raise ValueError("existing fight batch has different immutable content")
        seen, cursor = {batch_id}, next_id
        while cursor is not None:
            if cursor in seen:
                issue = "repeated navigation pointer; chain stopped"
                break
            seen.add(cursor)
            cursor = FightBatch.objects.filter(pk=cursor).values_list("next_id", flat=True).first()
        if not saved:
            saved = FightBatch.objects.create(id=batch_id, payload=payload, next_id=next_id, navigation_issue=issue)
        for row in payload["data"]:
            existing = Fight.objects.filter(pk=row["fight_id"]).first()
            if existing:
                if existing.payload != row:
                    raise ValueError(f"fight {existing.pk} has conflicting immutable input")
                continue
            ids = [row["blue_soldier_id"], row["red_soldier_id"]]
            if Person.objects.filter(pk__in=ids, health__isnull=False, rank__isnull=False).count() != 2:
                raise ValueError("fight references missing or undrafted soldiers")
            fight = Fight.objects.create(id=row["fight_id"], batch=saved,
                                         blue_id=ids[0], red_id=ids[1], payload=row)
            FightEvent.objects.bulk_create([
                FightEvent(fight=fight, position=i, attacker_id=e["attacker"],
                           defender_id=e["defender"], damage=e["damage"])
                for i, e in enumerate(row["steps"])
            ])
        return saved


def replay_fight(fight_id):
    with transaction.atomic():
        fight = Fight.objects.get(pk=fight_id)
        if fight.status == "completed":
            return False
        soldiers = {p.pk: p for p in Person.objects.filter(pk__in=[fight.blue_id, fight.red_id])}
        if len(soldiers) != 2 or any(p.health is None for p in soldiers.values()):
            raise ValueError("fight requires two soldiers with initialized health")
        fight.blue_start_health = soldiers[fight.blue_id].health
        fight.red_start_health = soldiers[fight.red_id].health
        for event in fight.events.order_by("position"):
            defender = soldiers[event.defender_id]
            defender.health -= event.damage
            if not -(2**63) <= defender.health <= 2**63 - 1:
                raise ValueError("health exceeded signed SQLite integer storage")
            Person.objects.filter(pk=defender.pk).update(health=defender.health)
            event.remaining_health = defender.health
            event.save(update_fields=["remaining_health"])
        fight.blue_end_health = soldiers[fight.blue_id].health
        fight.red_end_health = soldiers[fight.red_id].health
        fight.status, fight.error, fight.completed_at = "completed", "", timezone.now()
        fight.save(update_fields=["blue_start_health", "red_start_health", "blue_end_health",
                                  "red_end_health", "status", "error", "completed_at"])
        return True


def schedule_fights(batch_id=None):
    from core.tasks import simulate_fight
    fights = Fight.objects.exclude(status="completed")
    if batch_id is not None:
        fights = fights.filter(batch_id=batch_id)
    count = 0
    for fight_id in fights.values_list("pk", flat=True):
        simulate_fight.apply_async(args=[fight_id], retry=False)
        count += 1
    return count
