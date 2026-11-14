import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Person(models.Model):
    # Explicit primary key: IDs belong to the external API, including zero.
    id = models.IntegerField(
        primary_key=True, validators=[MinValueValidator(0), MaxValueValidator(99)]
    )
    name = models.TextField()
    age = models.PositiveIntegerField()
    key = models.TextField(blank=True, default="")
    rank = models.TextField(null=True, blank=True)
    # Initial draft input is separate from mutable health used by future combat.
    draft_health = models.PositiveIntegerField(null=True, blank=True)
    health = models.PositiveIntegerField(null=True, blank=True)
    draft_batch = models.ForeignKey(
        "DraftBatch", null=True, blank=True, on_delete=models.PROTECT,
        related_name="people",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id__gte=0, id__lte=99), name="person_id_range"
            ),
        ]

    def __str__(self):
        return self.name


class DraftBatch(models.Model):
    # A single authoritative draft stream. Revisions require a new explicit policy.
    id = models.PositiveBigIntegerField(primary_key=True)
    payload = models.JSONField(null=True)
    downloaded_at = models.DateTimeField(null=True)
    processed_at = models.DateTimeField(null=True)
    next_id = models.PositiveBigIntegerField(null=True)
    navigation_issue = models.TextField(blank=True)
    stage_path = models.TextField(blank=True)
    stage_info = models.JSONField(null=True)


class DraftJob(models.Model):
    """Transactional outbox and leased execution claim, independent of Celery IDs."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(DraftBatch, on_delete=models.PROTECT, related_name="jobs")
    kind = models.CharField(max_length=16, choices=[
        ("fetch", "fetch"), ("persist", "persist"), ("instructions", "instructions"),
    ])
    state = models.CharField(max_length=16, default="pending", choices=[
        (s, s) for s in ("pending", "published", "running", "succeeded", "failed")
    ])
    token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["batch", "kind"], name="unique_draft_batch_job",
        )]
