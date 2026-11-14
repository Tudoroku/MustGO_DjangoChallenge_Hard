import json

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError

from core.drafting import dispatch_jobs, draft_status, start_drafting
from core.models import DraftJob
from core.task_base import TransientNetworkError


class Command(BaseCommand):
    help = "Start/resume background draft discovery, or inspect durable status."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--status", action="store_true")
        group.add_argument("--resume", action="store_true", help="Republish pending/expired outbox work.")
        parser.add_argument("--retry-failed", action="store_true", help="Explicitly reset failed attempts.")

    def handle(self, *args, **options):
        if options["status"] and options["retry_failed"]:
            raise CommandError("--status cannot be combined with --retry-failed")
        try:
            if not options["status"]:
                if options["retry_failed"]:
                    DraftJob.objects.filter(state="failed").update(
                        state="pending", attempts=0, error="", token=None, lease_until=None,
                    )
                count = dispatch_jobs() if options["resume"] else start_drafting()
                self.stdout.write(f"Published {count} jobs. Use draft --status to inspect progress.")
            self.stdout.write(json.dumps(draft_status(), cls=DjangoJSONEncoder, indent=2))
        except (DatabaseError, TransientNetworkError) as exc:
            raise CommandError(f"Draft scheduling failed; durable jobs can be resumed: {exc}") from exc
