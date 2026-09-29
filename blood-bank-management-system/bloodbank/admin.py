from django.contrib import admin
from .models import *
for model in [Branch, UserProfile, Donor, Deferral, EligibilityRule, AppointmentSlot, Appointment, Donation, ShelfLifeRule, BloodUnit, TestType, LabResult, BloodRequest, RequestTransition, Reservation, CrossMatch, Issue, Notification, AuditEvent]: admin.site.register(model)
