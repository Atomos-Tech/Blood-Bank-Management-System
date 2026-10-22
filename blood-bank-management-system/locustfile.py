"""Locust load-test suite for BBMS (BB-38 / NFR-01 / TC-N01 and BB-39 / NFR-02 / TC-N02).

Simulates 100 concurrent authenticated users executing:
  - Dashboard overview requests (GET /)
  - Inventory search requests (GET /inventory/?blood_group=...&component=...&expires_before=...)
  - Inventory JSON API searches (GET /api/v1/inventory/?blood_group=...)
  - Concurrent stock reservation requests (POST /requests/<id>/reserve/)

Usage with Locust:
  locust -f locustfile.py --headless -u 100 -r 20 -t 15m --host http://127.0.0.1:8000 --csv=docs/test-results/locust
"""
import random
import re
from locust import HttpUser, between, task


BLOOD_GROUPS = ["O+", "O-", "A+", "A-", "B+", "B-", "AB+", "AB-"]
COMPONENTS = ["Packed Red Cells", "Platelets", "Plasma"]


class BBMSAuthenticatedUser(HttpUser):
    wait_time = between(0.2, 1.0)

    def on_start(self):
        self.email = "staff@bbms.local"
        self.password = "demo123"
        resp = self.client.get("/login/", name="GET /login/")
        csrf = self._extract_csrf(resp.text)
        self.client.post(
            "/login/",
            data={
                "email": self.email,
                "password": self.password,
                "csrfmiddlewaretoken": csrf,
            },
            headers={"Referer": f"{self.host}/login/"},
            name="POST /login/",
        )

    @staticmethod
    def _extract_csrf(html_text):
        match = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html_text)
        return match.group(1) if match else ""

    @task(4)
    def view_dashboard(self):
        with self.client.get("/", name="GET / (dashboard)", catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"Unexpected status {resp.status_code}")
            elif resp.elapsed.total_seconds() > 3.0:
                resp.failure(f"Exceeded 3s SLA: {resp.elapsed.total_seconds():.3f}s")

    @task(5)
    def search_inventory_html(self):
        bg = random.choice(BLOOD_GROUPS)
        comp = random.choice(COMPONENTS)
        with self.client.get(
            f"/inventory/?blood_group={bg}&component={comp}",
            name="GET /inventory/ (search)",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Unexpected status {resp.status_code}")
            elif resp.elapsed.total_seconds() > 3.0:
                resp.failure(f"Exceeded 3s SLA: {resp.elapsed.total_seconds():.3f}s")

    @task(3)
    def search_inventory_api(self):
        bg = random.choice(BLOOD_GROUPS)
        with self.client.get(
            f"/api/v1/inventory/?blood_group={bg}",
            name="GET /api/v1/inventory/",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"Unexpected status {resp.status_code}")
            elif resp.elapsed.total_seconds() > 3.0:
                resp.failure(f"Exceeded 3s SLA: {resp.elapsed.total_seconds():.3f}s")

    @task(1)
    def reserve_for_approved_request(self):
        req_page = self.client.get("/requests/", name="GET /requests/")
        csrf = self._extract_csrf(req_page.text)
        match = re.search(r'action="/requests/(\d+)/reserve/"', req_page.text)
        if match and csrf:
            req_id = match.group(1)
            with self.client.post(
                f"/requests/{req_id}/reserve/",
                data={"csrfmiddlewaretoken": csrf},
                headers={"Referer": f"{self.host}/requests/"},
                name="POST /requests/<id>/reserve/",
                catch_response=True,
            ) as resp:
                if resp.status_code not in (200, 302):
                    resp.failure(f"Unexpected status {resp.status_code}")
                elif resp.elapsed.total_seconds() > 5.0:
                    resp.failure(f"Reservation exceeded 5s SLA: {resp.elapsed.total_seconds():.3f}s")
