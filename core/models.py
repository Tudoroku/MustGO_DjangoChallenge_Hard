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

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id__gte=0, id__lte=99), name="person_id_range"
            ),
        ]

    def __str__(self):
        return self.name
