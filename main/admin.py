from django.contrib import admin
from .models import (
    Category, Attraction, AttractionService, ServiceBooking, AttractionReview,
    Event, EventBooking, EventReview,
    Hotel, Room, HotelBooking, HotelReview,
    Contact, Payment, Staff,
)

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name']


class AttractionServiceInline(admin.TabularInline):
    model  = AttractionService
    extra  = 1
    fields = ['name', 'service_type', 'price', 'capacity', 'duration', 'is_available']


@admin.register(Attraction)
class AttractionAdmin(admin.ModelAdmin):
    list_display  = ['name', 'category', 'location', 'entrance_fee', 'is_featured']
    list_filter   = ['category', 'is_featured']
    inlines       = [AttractionServiceInline]


@admin.register(AttractionService)
class AttractionServiceAdmin(admin.ModelAdmin):
    list_display  = ['name', 'attraction', 'service_type', 'price', 'capacity', 'duration', 'is_available']
    list_filter   = ['service_type', 'is_available', 'attraction']
    search_fields = ['name', 'attraction__name']


@admin.register(ServiceBooking)
class ServiceBookingAdmin(admin.ModelAdmin):
    list_display  = ['user', 'service', 'visit_date', 'num_guests', 'total_price', 'status']
    list_filter   = ['status']
    search_fields = ['user__username', 'service__name']


@admin.register(AttractionReview)
class AttractionReviewAdmin(admin.ModelAdmin):
    list_display = ['user', 'attraction', 'rating', 'created_at']
    list_filter  = ['rating']


class RoomInline(admin.TabularInline):
    model  = Room
    extra  = 1
    fields = ['room_number', 'room_type', 'price', 'max_guests', 'is_available']


@admin.register(Hotel)
class HotelAdmin(admin.ModelAdmin):
    list_display = ['name', 'address', 'stars', 'is_active']
    inlines      = [RoomInline]


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ['hotel', 'room_number', 'room_type', 'price', 'max_guests', 'is_available']
    list_filter  = ['hotel', 'room_type', 'is_available']


@admin.register(HotelBooking)
class HotelBookingAdmin(admin.ModelAdmin):
    list_display = ['user', 'room', 'check_in', 'check_out', 'total_price', 'status']
    list_filter  = ['status']


@admin.register(HotelReview)
class HotelReviewAdmin(admin.ModelAdmin):
    list_display = ['user', 'hotel', 'rating', 'created_at']
    list_filter  = ['rating']


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ['name', 'date', 'location', 'is_ticketed', 'ticket_price', 'ticket_capacity']
    list_filter  = ['is_ticketed']


@admin.register(EventBooking)
class EventBookingAdmin(admin.ModelAdmin):
    list_display = ['user', 'event', 'num_tickets', 'total_price', 'status']
    list_filter  = ['status']


@admin.register(EventReview)
class EventReviewAdmin(admin.ModelAdmin):
    list_display = ['event_booking', 'rating', 'created_at']


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display  = ['name', 'email', 'created_at']
    readonly_fields = ['name', 'email', 'message', 'created_at']


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display  = ['id', 'user', 'booking_type', 'amount', 'status', 'has_booking', 'created_at']
    list_filter   = ['status', 'booking_type']
    search_fields = ['user__username', 'paymongo_session_id']

    @admin.display(boolean=True, description='Booking created')
    def has_booking(self, obj):
        # A payment that is 'paid' but has NO booking needs a refund or manual rebooking.
        return bool(obj.hotel_booking_id or obj.event_booking_id or obj.service_booking_id)


@admin.register(Staff)
class StaffAdmin(admin.ModelAdmin):
    list_display = ['user', 'role', 'attraction', 'hotel', 'event']
    list_filter  = ['role']