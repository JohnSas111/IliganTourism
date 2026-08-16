from django.db import models
from django.contrib.auth.models import User


class Category(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        verbose_name_plural = 'Categories'

    def __str__(self):
        return self.name


class Attraction(models.Model):
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True)
    name = models.CharField(max_length=200)
    description = models.TextField()
    location = models.CharField(max_length=200)
    image = models.ImageField(upload_to='attractions/', blank=True, null=True)
    opening_hours = models.CharField(max_length=200, blank=True)
    entrance_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    is_featured = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def average_rating(self):
        reviews = self.attractionreview_set.all()
        if reviews.exists():
            return round(sum(r.rating for r in reviews) / reviews.count(), 1)
        return None


class AttractionService(models.Model):
    SERVICE_TYPES = [
        ('activity',      'Activity'),
        ('rental',        'Rental'),
        ('guide_service', 'Guide Service'),
        ('required_item', 'Required Item'),
        ('food_dining',   'Food & Dining'),
        ('accommodation', 'Accommodation'),
        ('day_package',   'Day Package'),
        ('group_package', 'Group Package'),
    ]
    attraction = models.ForeignKey(Attraction, on_delete=models.CASCADE, related_name='services')
    name = models.CharField(max_length=200)
    description = models.TextField()
    service_type = models.CharField(max_length=20, choices=SERVICE_TYPES, default='activity')
    price = models.DecimalField(max_digits=8, decimal_places=2)
    capacity = models.IntegerField(default=1, help_text='Max number of persons per booking')
    duration = models.CharField(max_length=100, blank=True, help_text='e.g. 1 hour, Overnight, All day')
    is_available = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.attraction.name} — {self.name}'


class ServiceBooking(models.Model):
    STATUS_CHOICES = [
        ('pending',   'Pending'),
        ('confirmed', 'Confirmed'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    service = models.ForeignKey(AttractionService, on_delete=models.CASCADE)
    visit_date = models.DateField()
    num_guests = models.IntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    total_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    reference_number = models.CharField(max_length=20, unique=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username} — {self.service.name}'


class AttractionReview(models.Model):
    attraction = models.ForeignKey(Attraction, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    rating = models.IntegerField(choices=[(i, i) for i in range(1, 6)])
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username} — {self.attraction.name} ({self.rating}★)'


class Event(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField()
    date = models.DateField()
    time = models.TimeField(blank=True, null=True, help_text='Event start time')
    image = models.ImageField(upload_to='events/', blank=True, null=True)
    location = models.CharField(max_length=200)
    is_ticketed = models.BooleanField(default=False)
    ticket_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    ticket_capacity = models.IntegerField(default=0, help_text='0 = unlimited')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def tickets_available(self):
        if self.ticket_capacity == 0:
            return True
        booked = self.eventbooking_set.filter(status__in=['pending', 'confirmed']).count()
        return booked < self.ticket_capacity


class EventBooking(models.Model):
    STATUS_CHOICES = [
        ('pending',   'Pending'),
        ('confirmed', 'Confirmed'),
        ('cancelled', 'Cancelled'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    event = models.ForeignKey(Event, on_delete=models.CASCADE)
    num_tickets = models.IntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    total_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    reference_number = models.CharField(max_length=20, unique=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username} — {self.event.name}'


class EventReview(models.Model):
    event_booking = models.ForeignKey(EventBooking, on_delete=models.CASCADE)
    rating = models.IntegerField(choices=[(i, i) for i in range(1, 6)])
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Review for {self.event_booking.event.name}'


class Hotel(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField()
    address = models.CharField(max_length=200)
    image = models.ImageField(upload_to='hotels/', blank=True, null=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    stars = models.IntegerField(choices=[(i, i) for i in range(1, 6)], default=3)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    def average_rating(self):
        reviews = self.hotelreview_set.all()
        if reviews.exists():
            return round(sum(r.rating for r in reviews) / reviews.count(), 1)
        return None


class Room(models.Model):
    ROOM_TYPES = [
        ('single', 'Single'),
        ('double', 'Double'),
        ('suite',  'Suite'),
        ('family', 'Family'),
        ('deluxe', 'Deluxe'),
    ]
    hotel = models.ForeignKey(Hotel, on_delete=models.CASCADE, related_name='rooms')
    room_number = models.CharField(max_length=10)
    room_type = models.CharField(max_length=20, choices=ROOM_TYPES, default='single')
    price = models.DecimalField(max_digits=8, decimal_places=2)
    max_guests = models.IntegerField(default=2)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to='rooms/', blank=True, null=True)
    is_available = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.hotel.name} — Room {self.room_number} ({self.room_type})'


class HotelBooking(models.Model):
    STATUS_CHOICES = [
        ('pending',   'Pending'),
        ('confirmed', 'Confirmed'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    room = models.ForeignKey(Room, on_delete=models.CASCADE)
    check_in = models.DateField()
    check_in_time = models.TimeField(blank=True, null=True, help_text='Expected check-in time')
    check_out = models.DateField()
    check_out_time = models.TimeField(blank=True, null=True, help_text='Expected check-out time')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    total_price = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    reference_number = models.CharField(max_length=20, unique=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username} — {self.room} ({self.check_in} to {self.check_out})'


class HotelReview(models.Model):
    hotel = models.ForeignKey(Hotel, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    booking = models.ForeignKey(HotelBooking, on_delete=models.SET_NULL, null=True, blank=True)
    rating = models.IntegerField(choices=[(i, i) for i in range(1, 6)])
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username} — {self.hotel.name} ({self.rating}★)'


class Contact(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.name} — {self.email}'


class Payment(models.Model):
    BOOKING_TYPES = [
        ('hotel',   'Hotel Booking'),
        ('event',   'Event Booking'),
        ('service', 'Service Booking'),
    ]
    STATUS_CHOICES = [
        ('pending',  'Pending'),
        ('paid',     'Paid'),
        ('failed',   'Failed'),
        ('expired',  'Expired'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    booking_type = models.CharField(max_length=10, choices=BOOKING_TYPES)
    # stores the booking pk temporarily before booking is created
    booking_data = models.JSONField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='PHP')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    # PayMongo checkout session ID
    paymongo_session_id = models.CharField(max_length=200, blank=True)
    paymongo_payment_id = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # after successful payment, store the actual booking id
    hotel_booking = models.ForeignKey(HotelBooking, null=True, blank=True, on_delete=models.SET_NULL)
    event_booking = models.ForeignKey(EventBooking, null=True, blank=True, on_delete=models.SET_NULL)
    service_booking = models.ForeignKey(ServiceBooking, null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return f'{self.user.username} — {self.booking_type} — {self.status}'


class Staff(models.Model):
    ROLE_CHOICES = [
        ('attraction', 'Attraction Staff'),
        ('hotel',      'Hotel Staff'),
        ('event',      'Event Staff'),
    ]
    user       = models.OneToOneField(User, on_delete=models.CASCADE, related_name='staff_profile')
    role       = models.CharField(max_length=15, choices=ROLE_CHOICES)
    attraction = models.ForeignKey(Attraction, null=True, blank=True, on_delete=models.SET_NULL)
    hotel      = models.ForeignKey(Hotel,      null=True, blank=True, on_delete=models.SET_NULL)
    event      = models.ForeignKey(Event,      null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user.username} — {self.get_role_display()}'

    def get_assigned_name(self):
        if self.role == 'attraction' and self.attraction:
            return self.attraction.name
        elif self.role == 'hotel' and self.hotel:
            return self.hotel.name
        elif self.role == 'event' and self.event:
            return self.event.name
        return 'Unassigned'
