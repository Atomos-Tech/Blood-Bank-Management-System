from datetime import timedelta
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone
from bloodbank.models import Branch, Donor, EligibilityRule, ShelfLifeRule, TestType, UserProfile, AppointmentSlot

class Command(BaseCommand):
    help = "Creates clearly labelled academic demonstration users and safe configuration."
    def handle(self,*args,**kwargs):
        branch,_=Branch.objects.get_or_create(code="MAIN",defaults={"name":"Main Demonstration Branch","timezone_name":"Asia/Kolkata","low_stock_threshold":5,"near_expiry_days":7})
        EligibilityRule.objects.get_or_create(active=True,defaults={"min_age":18,"max_age":65,"min_weight_kg":50,"minimum_interval_days":90})
        for component,days in [("Packed Red Cells",35),("Platelets",5),("Plasma",365)]: ShelfLifeRule.objects.get_or_create(component=component,defaults={"days":days})
        for name in ["HIV", "Hepatitis B", "Hepatitis C", "Syphilis", "Malaria"]: TestType.objects.get_or_create(name=name,defaults={"required":True})
        accounts=[("staff@bbms.local","Aarav","Staff",UserProfile.Role.STAFF,""),("hospital@bbms.local","Meera","Hospital",UserProfile.Role.HOSPITAL,"City Care Hospital"),("admin@bbms.local","Devika","Admin",UserProfile.Role.ADMIN,""),("donor@bbms.local","Ira","Donor",UserProfile.Role.DONOR,"")]
        for email,first,last,role,hospital in accounts:
            user,created=User.objects.get_or_create(username=email,defaults={"email":email,"first_name":first,"last_name":last})
            if created: user.set_password("demo123"); user.save()
            if role == UserProfile.Role.ADMIN:
                user.is_staff=True; user.is_superuser=True; user.save(update_fields=["is_staff","is_superuser"])
            UserProfile.objects.update_or_create(user=user,defaults={"role":role,"branch":branch,"hospital_name":hospital,"active":True})
            if role == UserProfile.Role.DONOR:
                Donor.objects.get_or_create(email=email,defaults={"user":user,"full_name":f"{first} {last}","phone":"9000000000","date_of_birth":"2001-02-15","weight_kg":60,"blood_group":"O+","consent_given":True})
        AppointmentSlot.objects.get_or_create(branch=branch,starts_at=timezone.now()+timedelta(days=3),defaults={"capacity":8})
        self.stdout.write(self.style.SUCCESS("Academic demo configuration created. Password for demo accounts: demo123"))
