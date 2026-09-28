import random
from datetime import date, timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models import User, Branch, Donor, Donation, BloodUnit, BloodRequest, BLOOD_GROUPS


class Command(BaseCommand):
    help = "Seed demo data for the Blood Bank Management System"

    def handle(self, *args, **kwargs):
        self.stdout.write("Seeding demo data...")

        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser("admin", "admin@bloodbank.test", "admin12345", role=User.Role.ADMIN)
            self.stdout.write(self.style.SUCCESS("Created admin / admin12345"))

        branches = []
        for name, city in [("City Central Blood Bank", "Bengaluru"), ("Northside Blood Bank", "Bengaluru")]:
            b, _ = Branch.objects.get_or_create(name=name, city=city, defaults={
                "address": f"{name} Main Road, {city}", "contact_phone": "080-1234567"
            })
            branches.append(b)

        staff_user, created = User.objects.get_or_create(username="staff1", defaults={
            "role": User.Role.STAFF, "email": "staff1@bloodbank.test",
        })
        if created:
            staff_user.set_password("staff12345")
            staff_user.save()
            self.stdout.write(self.style.SUCCESS("Created staff1 / staff12345"))

        hospital_user, created = User.objects.get_or_create(username="hospital1", defaults={
            "role": User.Role.HOSPITAL, "email": "hospital1@bloodbank.test",
        })
        if created:
            hospital_user.set_password("hospital12345")
            hospital_user.save()
            self.stdout.write(self.style.SUCCESS("Created hospital1 / hospital12345"))

        # Create donors + a few with a completed, passed donation (so inventory has stock)
        first_names = ["Rahul", "Priya", "Aman", "Sneha", "Vikram", "Anjali", "Karan", "Divya", "Rohit", "Meera"]
        donors = []
        for i, fname in enumerate(first_names):
            uname = f"donor{i+1}"
            user, created = User.objects.get_or_create(username=uname, defaults={
                "role": User.Role.DONOR, "email": f"{uname}@bloodbank.test", "first_name": fname,
            })
            if created:
                user.set_password("donor12345")
                user.save()
                Donor.objects.create(
                    user=user,
                    date_of_birth=date(1995 + (i % 15), (i % 12) + 1, (i % 27) + 1),
                    weight_kg=55 + i,
                    blood_group=BLOOD_GROUPS[i % len(BLOOD_GROUPS)],
                    branch=branches[i % 2],
                )
            donors.append(Donor.objects.get(user=user))

        self.stdout.write(self.style.SUCCESS(f"{len(donors)} donor accounts ready (password: donor12345)"))

        # Create completed donations -> passed test -> available units for first 8 donors
        today = timezone.now().date()
        for i, donor in enumerate(donors[:8]):
            donation_date = today - timedelta(days=random.randint(1, 20))
            donation, created = Donation.objects.get_or_create(
                donor=donor, donation_date=donation_date,
                defaults={"branch": donor.branch, "volume_ml": 450, "recorded_by": staff_user,
                          "test_result": Donation.TestResult.PASSED},
            )
            unit, _ = BloodUnit.objects.get_or_create(
                donation=donation,
                defaults={
                    "blood_group": donor.blood_group, "branch": donor.branch,
                    "collection_date": donation_date, "expiry_date": donation_date + timedelta(days=42),
                    "status": BloodUnit.Status.AVAILABLE,
                },
            )

        # One pending-test donation, and one sample hospital request
        if donors:
            pending_donor = donors[8] if len(donors) > 8 else donors[0]
            Donation.objects.get_or_create(
                donor=pending_donor, donation_date=today,
                defaults={"branch": pending_donor.branch, "volume_ml": 450, "recorded_by": staff_user},
            )

        BloodRequest.objects.get_or_create(
            hospital=hospital_user, blood_group="O+", units_needed=2, patient_name="Sample Patient — Demo",
            defaults={"branch": branches[0], "is_emergency": True},
        )

        self.stdout.write(self.style.SUCCESS("Demo data seeding complete."))
        self.stdout.write("\nLogin credentials:")
        self.stdout.write("  Admin:    admin / admin12345   (visit /admin/)")
        self.stdout.write("  Staff:    staff1 / staff12345")
        self.stdout.write("  Hospital: hospital1 / hospital12345")
        self.stdout.write("  Donor:    donor1 (etc.) / donor12345")
