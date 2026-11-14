from django.test import TestCase

from core.models import Person


class SimulatePageTests(TestCase):
    def test_html_shows_saved_drafts_without_replaying_or_mutating_health(self):
        Person.objects.create(id=0, name="<script>Soldier</script>", age=21,
                              rank="Lieutenant", draft_health=100, health=37)
        response = self.client.get("/simulate")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No fight data imported yet")
        self.assertContains(response, "Lieutenant")
        self.assertContains(response, "&lt;script&gt;Soldier&lt;/script&gt;")
        self.assertEqual(Person.objects.get(pk=0).health, 37)
        self.assertEqual(self.client.get("/simulate/").status_code, 200)
        self.assertEqual(self.client.post("/simulate").status_code, 405)
