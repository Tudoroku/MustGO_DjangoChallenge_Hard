import json

from django.core.management.base import BaseCommand, CommandError

from core.fighting import save_fight_batch, schedule_fights
from core.models import FightBatch
from core.tasks import fetch_fight_batch


class Command(BaseCommand):
    help = "Import actual fight batches and schedule one Celery task per fight."

    def add_arguments(self, parser):
        parser.add_argument("--file")
        parser.add_argument("--batch-id", type=int, default=0)
        parser.add_argument("--resume", action="store_true")

    def handle(self, *args, **options):
        try:
            if options["file"]:
                with open(options["file"], "rb") as source:
                    payload = json.load(source)
                batch = save_fight_batch(options["batch_id"], payload)
                count = schedule_fights(batch.pk)
                self.stdout.write(f"Saved batch {batch.pk}; scheduled {count} fights. File mode does not fetch successors.")
            elif options["resume"]:
                count = schedule_fights()
                for batch in FightBatch.objects.filter(navigation_issue="", next_id__isnull=False):
                    if not FightBatch.objects.filter(pk=batch.next_id).exists():
                        fetch_fight_batch.apply_async(args=[batch.next_id], retry=False)
                self.stdout.write(f"Rescheduled {count} incomplete fights and missing successor downloads.")
            else:
                result = fetch_fight_batch.apply_async(args=[options["batch_id"]], retry=False)
                self.stdout.write(f"Background fight import started: {result.id}")
        except Exception as exc:
            raise CommandError(f"Fight import failed (saved work can be resumed): {exc}") from exc
