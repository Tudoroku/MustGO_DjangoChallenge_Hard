import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from core.drafting import claim_job, dispatch_jobs, persist_batch, save_download
from core.models import DraftJob
from core.task_base import TransientNetworkError


class Command(BaseCommand):
    help = "Import a saved numbered draft response without discovering/fetching successors."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--batch-id", required=True, type=int)
        parser.add_argument("--sync", action="store_true", help="Persist locally without a broker or HTTP.")

    def handle(self, *args, **options):
        try:
            with open(options["file"], "rb") as source:
                payload = json.load(source)
            batch = save_download(options["batch_id"], payload)
            if options["sync"]:
                job = DraftJob.objects.get(batch=batch, kind="persist")
                claimed = claim_job(job.pk)
                if claimed:
                    try:
                        persist_batch(claimed)
                    except Exception as exc:
                        DraftJob.objects.filter(pk=claimed.pk, token=claimed.token).update(
                            state="failed", error=str(exc), lease_until=None,
                        )
                        raise
            else:
                dispatch_jobs()
        except (OSError, ValueError, ValidationError, DatabaseError, TransientNetworkError) as exc:
            raise CommandError(f"Could not import draft batch: {exc}") from exc
        self.stdout.write(f"Saved draft batch {batch.pk}. Completion remains unverified.")
