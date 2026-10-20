from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django import forms
from django.contrib import messages
from django.utils import timezone
from django.core.paginator import Paginator
from django.db import models, transaction
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
import logging
import random, re, string

def _generate_reference(prefix):
    chars = string.ascii_uppercase + string.digits
    return prefix + '-' + ''.join(random.choices(chars, k=8))
from . import assistant, geo
from .models import (
    Category, Attraction, AttractionService, ServiceBooking, AttractionReview,
    Event, EventBooking,
    Hotel, Room, HotelBooking, HotelReview,
    Contact
)


# ── Input validation helpers (Batch 2B) ──────────────────────────────────
MAX_TICKETS_PER_BOOKING = 10   # matches the limit already shown on the booking form


def _parse_int(value, minimum=1, maximum=None):
    """Return an int within [minimum, maximum], or None if the input is not valid."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number < minimum or (maximum is not None and number > maximum):
        return None
    return number


def _parse_date(value):
    """Return a date from an ISO string (YYYY-MM-DD), or None if invalid."""
    from datetime import date as date_type
    try:
        return date_type.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_time(value):
    """Return (ok, value). Empty input is fine (None); otherwise it must be HH:MM."""
    from datetime import time as time_type
    if not value:
        return True, None
    try:
        time_type.fromisoformat(str(value).strip())
    except ValueError:
        return False, None
    return True, str(value).strip()


# ── Maps & weather (Batch 3) ──────────────────────────────────────────────
def _parse_coordinate(raw, limit):
    """A Decimal within +/-limit rounded to 6 places, or None if it is not valid."""
    from decimal import Decimal, InvalidOperation
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    if not value.is_finite() or abs(value) > limit:
        return None
    return value.quantize(Decimal('0.000001'))


def _apply_coordinates(request, obj, search_texts):
    """
    Set obj.latitude/longitude from the admin form. If the admin left both blank,
    try to find the place automatically using each wording in `search_texts`,
    most specific first. Never blocks saving the record.
    """
    lat_raw = request.POST.get('latitude', '').strip()
    lng_raw = request.POST.get('longitude', '').strip()
    if lat_raw and not lng_raw:
        # Google Maps copies both numbers as one text ("8.18, 124.19"); accept that pasted into Latitude.
        pieces = [piece for piece in re.split(r'[,;\s]+', lat_raw) if piece]
        if len(pieces) == 2:
            lat_raw, lng_raw = pieces
    if lat_raw or lng_raw:
        lat, lng = _parse_coordinate(lat_raw, 90), _parse_coordinate(lng_raw, 180)
        if lat is None or lng is None:
            messages.warning(
                request,
                'The map coordinates were not saved. Latitude must be a number from -90 to 90 '
                'and longitude a number from -180 to 180.')
            return
        obj.latitude, obj.longitude = lat, lng
        return
    obj.latitude = obj.longitude = None
    found = geo.geocode_any(search_texts)
    if found:
        lat, lng = _parse_coordinate(found[0], 90), _parse_coordinate(found[1], 180)
        if lat is not None and lng is not None:
            obj.latitude, obj.longitude = lat, lng
            messages.info(
                request,
                f'Map location found automatically by searching for "{found[2]}" ({lat}, {lng}). '
                'Please check that the pin is correct.')
            return
    searched = '; '.join(f'"{text}"' for text in dict.fromkeys(t for t in search_texts if t))
    messages.warning(
        request,
        f'Saved, but no map location could be found automatically (searched for: {searched}). '
        'Edit it and enter the latitude and longitude yourself.')


@require_GET
def weather_api(request, kind, pk):
    """JSON forecast for an attraction, hotel or event page (used by the page's weather box)."""
    model = {'attraction': Attraction, 'hotel': Hotel, 'event': Event}.get(kind)
    if model is None:
        return JsonResponse({'ok': False, 'error': 'unknown_type'}, status=404)
    obj = model.objects.filter(pk=pk).first()
    if obj is None or obj.latitude is None or obj.longitude is None:
        return JsonResponse({'ok': False, 'error': 'no_location'}, status=404)
    forecast = geo.fetch_forecast(obj.latitude, obj.longitude)
    if forecast is None:
        return JsonResponse({'ok': False, 'error': 'unavailable'}, status=503)
    if kind == 'event':
        wanted = obj.date.isoformat()
        match = next((day for day in forecast['days'] if day['date'] == wanted), None)
        return JsonResponse({
            'ok': True, 'current': forecast['current'],
            'days': [match] if match else [],
            'event_date': wanted, 'event_in_range': match is not None,
        })
    return JsonResponse({'ok': True, 'current': forecast['current'], 'days': forecast['days'][:7]})


# ── AI tourism assistant (Batch 4) ────────────────────────────────────────
def _assistant_reply(ok, status, **data):
    return JsonResponse({'ok': ok, **data}, status=status)


@never_cache
@require_POST
def assistant_ask(request):
    """Answer a visitor's question with Gemini, using only public tourism data."""
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        return _assistant_reply(False, 400, error='bad_request', message='Please send your question as text.')
    question = body.get('message')
    if not isinstance(question, str) or not question.strip():
        return _assistant_reply(False, 400, error='empty', message='Please type a question.')
    if len(question) > assistant.MAX_QUESTION_CHARS:
        return _assistant_reply(
            False, 400, error='too_long',
            message=f'Please keep your question under {assistant.MAX_QUESTION_CHARS} characters.')
    history = body.get('history')
    if not isinstance(history, list):
        history = []
    try:
        if not settings.GEMINI_API_KEY:
            raise assistant.AssistantError('not_configured')
        assistant.check_rate_limit(assistant.client_key(request))
        answer = assistant.answer_question(question, history)
    except assistant.AssistantError as problem:
        return _assistant_reply(False, problem.status, error=problem.code, message=problem.message)
    return _assistant_reply(True, 200, answer=answer)


class RegisterForm(UserCreationForm):
    first_name = forms.CharField(max_length=50, required=True)
    last_name  = forms.CharField(max_length=50, required=True)
    email      = forms.EmailField(required=True)

    class Meta:
        model  = User
        fields = ['first_name', 'last_name', 'username', 'email', 'password1', 'password2']

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data['first_name']
        user.last_name  = self.cleaned_data['last_name']
        user.email      = self.cleaned_data['email']
        if commit:
            user.save()
        return user


# ── HOME ──────────────────────────────────────────
def home(request):
    featured_attractions = Attraction.objects.filter(is_featured=True)[:6]
    upcoming_events      = Event.objects.filter(date__gte=timezone.now().date()).order_by('date')[:6]
    hotels               = Hotel.objects.filter(is_active=True)[:6]
    categories           = Category.objects.all()
    return render(request, 'main/home.html', {
        'featured_attractions': featured_attractions,
        'upcoming_events':      upcoming_events,
        'hotels':               hotels,
        'categories':           categories,
    })


# ── ATTRACTIONS ───────────────────────────────────
def attractions_list(request):
    category_id = request.GET.get('category')
    attractions = Attraction.objects.all()
    if category_id:
        attractions = attractions.filter(category_id=category_id)
    categories = Category.objects.all()
    return render(request, 'main/attractions_list.html', {
        'attractions':       attractions,
        'categories':        categories,
        'selected_category': int(category_id) if category_id else None,
    })


def attraction_detail(request, pk):
    attraction = get_object_or_404(Attraction, pk=pk)

    # Group services by type for organised display
    services_by_type = {}
    for service in attraction.services.filter(is_available=True).order_by('service_type', 'name'):
        label = service.get_service_type_display()
        services_by_type.setdefault(label, []).append(service)

    reviews = attraction.attractionreview_set.select_related('user').order_by('-created_at')

    # Check if user has a completed/confirmed booking at this attraction
    user_has_booked = False
    if request.user.is_authenticated:
        user_has_booked = ServiceBooking.objects.filter(
            user=request.user,
            service__attraction=attraction,
            status__in=('confirmed', 'completed'),
        ).exists()

    # Handle review submission
    if request.method == 'POST' and request.user.is_authenticated:
        if not user_has_booked:
            messages.error(request, 'You must have booked a service at this attraction to leave a review.')
            return redirect('attraction_detail', pk=pk)
        rating  = _parse_int(request.POST.get('rating'), minimum=1, maximum=5)
        comment = request.POST.get('comment', '').strip()
        if rating and comment:
            AttractionReview.objects.create(
                attraction=attraction,
                user=request.user,
                rating=rating,
                comment=comment,
            )
            messages.success(request, 'Review submitted!')
            return redirect('attraction_detail', pk=pk)
        messages.error(request, 'Please choose a rating from 1 to 5 and write a comment.')

    return render(request, 'main/attraction_detail.html', {
        'attraction':       attraction,
        'services_by_type': services_by_type,
        'reviews':          reviews,
        'user_has_booked':  user_has_booked,
    })


@login_required
def book_service(request, service_id):
    from datetime import date as date_type
    service = get_object_or_404(AttractionService, pk=service_id, is_available=True)
    today = date_type.today().isoformat()
    if request.method == 'POST':
        visit_date_value = _parse_date(request.POST.get('visit_date'))
        max_guests       = max(service.capacity, 1)
        num_guests       = _parse_int(request.POST.get('num_guests', 1), minimum=1, maximum=max_guests)
        notes            = request.POST.get('notes', '')
        if visit_date_value is None:
            messages.error(request, 'Please choose a valid visit date.')
            return render(request, 'main/book_service.html', {'service': service, 'today': today})
        if visit_date_value < date_type.today():
            messages.error(request, 'Visit date cannot be in the past.')
            return render(request, 'main/book_service.html', {'service': service, 'today': today})
        if num_guests is None:
            messages.error(request, f'Number of guests must be a whole number from 1 to {max_guests}.')
            return render(request, 'main/book_service.html', {'service': service, 'today': today})
        visit_date  = visit_date_value.isoformat()
        total_price = service.price * num_guests
        request.session['pending_booking'] = {
            'type':        'service',
            'service_id':  service_id,
            'visit_date':  visit_date,
            'num_guests':  num_guests,
            'notes':       notes,
            'total_price': str(total_price),
        }
        return redirect('payment_checkout')
    return render(request, 'main/book_service.html', {'service': service, 'today': today})

# ── EVENTS ────────────────────────────────────────
def events_list(request):
    events      = Event.objects.filter(date__gte=timezone.now().date()).order_by('date')
    past_events = Event.objects.filter(date__lt=timezone.now().date()).order_by('-date')[:3]
    return render(request, 'main/events_list.html', {
        'events':      events,
        'past_events': past_events,
    })


def event_detail(request, pk):
    event = get_object_or_404(Event, pk=pk)
    user_booked = False
    if request.user.is_authenticated:
        user_booked = EventBooking.objects.filter(
            user=request.user, event=event, status__in=['pending', 'confirmed']
        ).exists()
    return render(request, 'main/event_detail.html', {
        'event':       event,
        'user_booked': user_booked,
    })


@login_required
def book_event(request, event_id):
    from datetime import date as date_type
    event = get_object_or_404(Event, pk=event_id, is_ticketed=True)
    if request.method == 'POST':
        num_tickets = _parse_int(request.POST.get('num_tickets', 1), minimum=1, maximum=MAX_TICKETS_PER_BOOKING)
        if event.date < date_type.today():
            messages.error(request, 'This event has already passed.')
            return render(request, 'main/book_event.html', {'event': event})
        if num_tickets is None:
            messages.error(request, f'Number of tickets must be a whole number from 1 to {MAX_TICKETS_PER_BOOKING}.')
            return render(request, 'main/book_event.html', {'event': event})
        total_price = event.ticket_price * num_tickets
        # Check capacity
        if event.ticket_capacity > 0:
            booked = EventBooking.objects.filter(
                event=event, status__in=('confirmed', 'pending')
            ).aggregate(total=models.Sum('num_tickets'))['total'] or 0
            if booked + num_tickets > event.ticket_capacity:
                remaining = event.ticket_capacity - booked
                if remaining <= 0:
                    messages.error(request, 'Sorry, this event is sold out.')
                else:
                    messages.error(request, f'Only {remaining} ticket(s) remaining.')
                return render(request, 'main/book_event.html', {'event': event})
        request.session['pending_booking'] = {
            'type':        'event',
            'event_id':    event_id,
            'num_tickets': num_tickets,
            'total_price': str(total_price),
        }
        return redirect('payment_checkout')
    # Pass remaining tickets to template
    tickets_remaining = None
    if event.ticket_capacity > 0:
        booked = EventBooking.objects.filter(
            event=event, status__in=('confirmed', 'pending')
        ).aggregate(total=models.Sum('num_tickets'))['total'] or 0
        tickets_remaining = max(0, event.ticket_capacity - booked)
    return render(request, 'main/book_event.html', {
        'event': event,
        'tickets_remaining': tickets_remaining,
    })

# ── HOTELS ────────────────────────────────────────
def hotels_list(request):
    hotels = Hotel.objects.filter(is_active=True)
    return render(request, 'main/hotels_list.html', {'hotels': hotels})


def hotel_detail(request, pk):
    hotel   = get_object_or_404(Hotel, pk=pk)
    rooms   = hotel.rooms.filter(is_available=True)
    reviews = hotel.hotelreview_set.select_related('user').order_by('-created_at')

    # Collect booked date ranges per room for display
    from collections import defaultdict
    booked_dates = defaultdict(list)
    for b in HotelBooking.objects.filter(room__hotel=hotel, status='confirmed').values('room_id', 'check_in', 'check_out'):
        booked_dates[b['room_id']].append((b['check_in'], b['check_out']))

    for room in rooms:
        room.booked_ranges = booked_dates.get(room.pk, [])
        room.is_booked = len(room.booked_ranges) > 0

    return render(request, 'main/hotel_detail.html', {
        'hotel':   hotel,
        'rooms':   rooms,
        'reviews': reviews,
    })


def room_detail(request, pk):
    room = get_object_or_404(Room, pk=pk, is_available=True)
    return render(request, 'main/room_detail.html', {'room': room})


@login_required
def book_room(request, room_id):
    from datetime import date as date_type, timedelta
    room     = get_object_or_404(Room, pk=room_id, is_available=True)
    today    = date_type.today()
    tomorrow = today + timedelta(days=1)
    ctx = {'room': room, 'today': today.isoformat(), 'tomorrow': tomorrow.isoformat()}
    if request.method == 'POST':
        check_in_date  = _parse_date(request.POST.get('check_in'))
        check_out_date = _parse_date(request.POST.get('check_out'))
        if check_in_date is None or check_out_date is None:
            messages.error(request, 'Please choose valid check-in and check-out dates.')
            return render(request, 'main/book_room.html', ctx)
        times_ok_in,  check_in_time  = _parse_time(request.POST.get('check_in_time'))
        times_ok_out, check_out_time = _parse_time(request.POST.get('check_out_time'))
        if not (times_ok_in and times_ok_out):
            messages.error(request, 'Please enter valid check-in and check-out times.')
            return render(request, 'main/book_room.html', ctx)
        check_in  = check_in_date.isoformat()
        check_out = check_out_date.isoformat()
        if check_in_date < today:
            messages.error(request, 'Check-in date cannot be in the past.')
            return render(request, 'main/book_room.html', ctx)
        if check_out_date <= check_in_date:
            messages.error(request, 'Check-out date must be after check-in date.')
            return render(request, 'main/book_room.html', ctx)
        # Check for date conflicts with existing confirmed bookings
        conflict = HotelBooking.objects.filter(
            room_id=room_id,
            status='confirmed',
            check_in__lt=check_out_date,
            check_out__gt=check_in_date,
        ).exists()
        if conflict:
            messages.error(request, 'This room is already booked for the selected dates. Please choose different dates.')
            return render(request, 'main/book_room.html', ctx)
        nights      = (check_out_date - check_in_date).days
        total_price = room.price * nights
        request.session['pending_booking'] = {
            'type':           'hotel',
            'room_id':        room_id,
            'check_in':       check_in,
            'check_in_time':  check_in_time,
            'check_out':      check_out,
            'check_out_time': check_out_time,
            'total_price':    str(total_price),
        }
        return redirect('payment_checkout')
    return render(request, 'main/book_room.html', ctx)

# ── MY BOOKINGS ───────────────────────────────────
@login_required
def my_bookings(request):
    search = request.GET.get('q', '').strip()

    hotel_qs = HotelBooking.objects.filter(user=request.user).select_related('room__hotel').order_by('-created_at')
    event_qs = EventBooking.objects.filter(user=request.user).select_related('event').order_by('-created_at')
    service_qs = ServiceBooking.objects.filter(user=request.user).select_related('service__attraction').order_by('-created_at')

    if search:
        hotel_qs = hotel_qs.filter(room__hotel__name__icontains=search)
        event_qs = event_qs.filter(event__name__icontains=search)
        service_qs = service_qs.filter(service__attraction__name__icontains=search)

    hotel_page   = Paginator(hotel_qs,   10).get_page(request.GET.get('hotel_page'))
    event_page   = Paginator(event_qs,   10).get_page(request.GET.get('event_page'))
    service_page = Paginator(service_qs, 10).get_page(request.GET.get('service_page'))

    return render(request, 'main/my_bookings.html', {
        'hotel_bookings':   hotel_page,
        'event_bookings':   event_page,
        'service_bookings': service_page,
        'search':           search,
    })


@login_required
def booking_detail(request, booking_type, pk):
    if booking_type == 'hotel':
        booking = get_object_or_404(HotelBooking, pk=pk, user=request.user)
    elif booking_type == 'event':
        booking = get_object_or_404(EventBooking, pk=pk, user=request.user)
    elif booking_type == 'service':
        booking = get_object_or_404(ServiceBooking, pk=pk, user=request.user)
    else:
        return redirect('my_bookings')
    return render(request, 'main/booking_detail.html', {
        'booking': booking,
        'booking_type': booking_type,
    })


CANCEL_WINDOW_HOURS = 24

def _is_within_cancel_window(booking):
    """Return True if booking was created within the cancellation window."""
    from datetime import timedelta
    deadline = booking.created_at + timedelta(hours=CANCEL_WINDOW_HOURS)
    return timezone.now() <= deadline


def _booking_start_date(booking):
    """The date the customer's visit starts."""
    if isinstance(booking, HotelBooking):
        return booking.check_in
    if isinstance(booking, EventBooking):
        return booking.event.date
    return booking.visit_date


def _cancel_booking(request, booking, label):
    """Shared cancellation rules for hotel, event and service bookings."""
    from datetime import date as date_type
    if booking.status not in ('confirmed', 'pending'):
        messages.error(request, 'This booking cannot be cancelled.')
    elif _booking_start_date(booking) < date_type.today():
        messages.error(request, 'This booking date has already passed, so it can no longer be cancelled.')
    elif not _is_within_cancel_window(booking):
        messages.error(request, f'Cancellation window has passed. Bookings can only be cancelled within {CANCEL_WINDOW_HOURS} hours of booking.')
    else:
        booking.status = 'cancelled'
        booking.save()
        # NOTE: no automatic refund is issued by this system. Refunds are handled by staff.
        messages.success(
            request,
            f'{label} booking cancelled. Refunds are reviewed and processed by our team. '
            f'Please contact us and quote reference {booking.reference_number}.'
        )
    return redirect('my_bookings')


@login_required
def cancel_hotel_booking(request, pk):
    if request.method != 'POST':
        return redirect('my_bookings')
    booking = get_object_or_404(HotelBooking, pk=pk, user=request.user)
    return _cancel_booking(request, booking, 'Hotel')


@login_required
def cancel_event_booking(request, pk):
    if request.method != 'POST':
        return redirect('my_bookings')
    booking = get_object_or_404(EventBooking, pk=pk, user=request.user)
    return _cancel_booking(request, booking, 'Event')


@login_required
def cancel_service_booking(request, pk):
    if request.method != 'POST':
        return redirect('my_bookings')
    booking = get_object_or_404(ServiceBooking, pk=pk, user=request.user)
    return _cancel_booking(request, booking, 'Attraction service')


# ── AUTH ──────────────────────────────────────────
def register(request):
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, f'Welcome, {user.first_name}! Your account has been created.')
            return redirect('home')
    else:
        form = RegisterForm()
    return render(request, 'registration/register.html', {'form': form})


# ── CONTACT ───────────────────────────────────────
def contact(request):
    if request.method == 'POST':
        Contact.objects.create(
            name=request.POST.get('name'),
            email=request.POST.get('email'),
            message=request.POST.get('message'),
        )
        messages.success(request, 'Message sent! We will get back to you soon.')
        return redirect('contact')
    return render(request, 'main/contact.html')


# ── CUSTOM LOGIN (redirects staff to admin panel) ─
def custom_login(request):
    if request.user.is_authenticated:
        if hasattr(request.user, 'staff_profile'):
            return redirect('staff_dashboard')
        if request.user.is_staff:
            return redirect('admin_dashboard')
        return redirect('home')
    if request.method == 'POST':
        from django.contrib.auth import authenticate, login as auth_login
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user:
            auth_login(request, user)
            # Check staff_profile FIRST - staff accounts should go to staff dashboard
            if hasattr(user, 'staff_profile'):
                return redirect('staff_dashboard')
            if user.is_staff:
                return redirect('admin_dashboard')
            return redirect('home')
        else:
            return render(request, 'registration/login.html', {'login_error': True})
    return render(request, 'registration/login.html')


# ── CUSTOM ADMIN PANEL ────────────────────────────

from functools import wraps
from django.contrib.auth.models import User

def staff_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.is_staff:
            return redirect('home')
        return view_func(request, *args, **kwargs)
    return wrapper


@staff_required
def admin_dashboard(request):
    total_users       = User.objects.count()
    total_attractions = Attraction.objects.count()
    featured_count    = Attraction.objects.filter(is_featured=True).count()
    upcoming_events   = Event.objects.filter(date__gte=timezone.now().date()).count()
    total_bookings    = (
        HotelBooking.objects.count() +
        EventBooking.objects.count() +
        ServiceBooking.objects.count()
    )
    return render(request, 'admin_panel/dashboard.html', {
        'active':                 'dashboard',
        'total_users':            total_users,
        'total_attractions':      total_attractions,
        'featured_count':         featured_count,
        'upcoming_events':        upcoming_events,
        'total_bookings':         total_bookings,
        'recent_hotel_bookings':  HotelBooking.objects.select_related('user','room__hotel').order_by('-created_at')[:5],
        'recent_event_bookings':  EventBooking.objects.select_related('user','event').order_by('-created_at')[:5],
        'recent_service_bookings':ServiceBooking.objects.select_related('user','service').order_by('-created_at')[:5],
        'recent_users':           User.objects.order_by('-date_joined')[:5],
    })


# ── Attractions ──
@staff_required
def admin_attractions(request):
    return render(request, 'admin_panel/attractions.html', {
        'active': 'attractions',
        'attractions': Attraction.objects.select_related('category').order_by('-created_at'),
    })

@staff_required
def admin_attraction_add(request):
    if request.method == 'POST':
        a = Attraction(
            name=request.POST.get('name'),
            description=request.POST.get('description',''),
            location=request.POST.get('location'),
            opening_hours=request.POST.get('opening_hours',''),
            entrance_fee=request.POST.get('entrance_fee', 0),
            is_featured='is_featured' in request.POST,
        )
        cat_id = request.POST.get('category')
        if cat_id:
            a.category_id = cat_id
        if 'image' in request.FILES:
            a.image = request.FILES['image']
        _apply_coordinates(request, a, [f'{a.name}, {a.location}', a.name, a.location])
        a.save()
        messages.success(request, f'Attraction "{a.name}" added successfully.')
        return redirect('admin_attractions')
    return render(request, 'admin_panel/attraction_form.html', {
        'active': 'attractions',
        'categories': Category.objects.all(),
    })

@staff_required
def admin_attraction_edit(request, pk):
    a = get_object_or_404(Attraction, pk=pk)
    if request.method == 'POST':
        a.name          = request.POST.get('name')
        a.description   = request.POST.get('description','')
        a.location      = request.POST.get('location')
        a.opening_hours = request.POST.get('opening_hours','')
        a.entrance_fee  = request.POST.get('entrance_fee', 0)
        a.is_featured   = 'is_featured' in request.POST
        cat_id = request.POST.get('category')
        a.category_id = cat_id if cat_id else None
        if 'image' in request.FILES:
            a.image = request.FILES['image']
        _apply_coordinates(request, a, [f'{a.name}, {a.location}', a.name, a.location])
        a.save()
        messages.success(request, f'Attraction "{a.name}" updated.')
        return redirect('admin_attractions')
    return render(request, 'admin_panel/attraction_form.html', {
        'active': 'attractions',
        'attraction': a,
        'categories': Category.objects.all(),
    })

@staff_required
def admin_attraction_delete(request, pk):
    a = get_object_or_404(Attraction, pk=pk)
    if request.method == 'POST':
        name = a.name
        a.delete()
        messages.success(request, f'Attraction "{name}" deleted.')
        return redirect('admin_attractions')
    return render(request, 'admin_panel/confirm_delete.html', {
        'active': 'attractions',
        'object_type': 'Attraction',
        'object_name': a.name,
        'cancel_url': '/panel/attractions/',
    })


# ── Hotels ──
@staff_required
def admin_hotels(request):
    return render(request, 'admin_panel/hotels.html', {
        'active': 'hotels',
        'hotels': Hotel.objects.prefetch_related('rooms').order_by('-created_at'),
    })

@staff_required
def admin_hotel_add(request):
    if request.method == 'POST':
        h = Hotel(
            name=request.POST.get('name'),
            description=request.POST.get('description',''),
            address=request.POST.get('address'),
            phone=request.POST.get('phone',''),
            email=request.POST.get('email',''),
            stars=int(request.POST.get('stars', 3)),
            is_active='is_active' in request.POST,
        )
        if 'image' in request.FILES:
            h.image = request.FILES['image']
        _apply_coordinates(request, h, [f'{h.name}, {h.address}', h.name, h.address])
        h.save()
        messages.success(request, f'Hotel "{h.name}" added successfully.')
        return redirect('admin_hotels')
    return render(request, 'admin_panel/hotel_form.html', {'active': 'hotels'})

@staff_required
def admin_hotel_edit(request, pk):
    h = get_object_or_404(Hotel, pk=pk)
    if request.method == 'POST':
        h.name        = request.POST.get('name')
        h.description = request.POST.get('description','')
        h.address     = request.POST.get('address')
        h.phone       = request.POST.get('phone','')
        h.email       = request.POST.get('email','')
        h.stars       = int(request.POST.get('stars', 3))
        h.is_active   = 'is_active' in request.POST
        if 'image' in request.FILES:
            h.image = request.FILES['image']
        _apply_coordinates(request, h, [f'{h.name}, {h.address}', h.name, h.address])
        h.save()
        messages.success(request, f'Hotel "{h.name}" updated.')
        return redirect('admin_hotels')
    return render(request, 'admin_panel/hotel_form.html', {'active': 'hotels', 'hotel': h})

@staff_required
def admin_hotel_delete(request, pk):
    h = get_object_or_404(Hotel, pk=pk)
    if request.method == 'POST':
        name = h.name
        h.delete()
        messages.success(request, f'Hotel "{name}" deleted.')
        return redirect('admin_hotels')
    return render(request, 'admin_panel/confirm_delete.html', {
        'active': 'hotels',
        'object_type': 'Hotel',
        'object_name': h.name,
        'cancel_url': '/panel/hotels/',
    })


# ── Events ──
@staff_required
def admin_events(request):
    return render(request, 'admin_panel/events.html', {
        'active': 'events',
        'events': Event.objects.order_by('-date'),
    })

@staff_required
def admin_event_add(request):
    if request.method == 'POST':
        e = Event(
            name=request.POST.get('name'),
            description=request.POST.get('description',''),
            date=request.POST.get('date'),
            time=request.POST.get('time') or None,
            location=request.POST.get('location'),
            is_ticketed='is_ticketed' in request.POST,
            ticket_price=request.POST.get('ticket_price', 0),
            ticket_capacity=request.POST.get('ticket_capacity', 0),
        )
        if 'image' in request.FILES:
            e.image = request.FILES['image']
        _apply_coordinates(request, e, [e.location])
        e.save()
        messages.success(request, f'Event "{e.name}" added successfully.')
        return redirect('admin_events')
    return render(request, 'admin_panel/event_form.html', {'active': 'events'})

@staff_required
def admin_event_edit(request, pk):
    e = get_object_or_404(Event, pk=pk)
    if request.method == 'POST':
        e.name            = request.POST.get('name')
        e.description     = request.POST.get('description','')
        e.date            = request.POST.get('date')
        e.time            = request.POST.get('time') or None
        e.location        = request.POST.get('location')
        e.is_ticketed     = 'is_ticketed' in request.POST
        e.ticket_price    = request.POST.get('ticket_price', 0)
        e.ticket_capacity = request.POST.get('ticket_capacity', 0)
        if 'image' in request.FILES:
            e.image = request.FILES['image']
        _apply_coordinates(request, e, [e.location])
        e.save()
        messages.success(request, f'Event "{e.name}" updated.')
        return redirect('admin_events')
    return render(request, 'admin_panel/event_form.html', {'active': 'events', 'event': e})

@staff_required
def admin_event_delete(request, pk):
    e = get_object_or_404(Event, pk=pk)
    if request.method == 'POST':
        name = e.name
        e.delete()
        messages.success(request, f'Event "{name}" deleted.')
        return redirect('admin_events')
    return render(request, 'admin_panel/confirm_delete.html', {
        'active': 'events',
        'object_type': 'Event',
        'object_name': e.name,
        'cancel_url': '/panel/events/',
    })


# ── Bookings ──
@staff_required
def admin_hotel_bookings(request):
    search = request.GET.get('q', '').strip()
    qs = HotelBooking.objects.select_related('user', 'room__hotel').order_by('-created_at')
    if search:
        qs = qs.filter(
            models.Q(user__username__icontains=search) |
            models.Q(user__first_name__icontains=search) |
            models.Q(user__last_name__icontains=search) |
            models.Q(room__hotel__name__icontains=search) |
            models.Q(room__room_number__icontains=search)
        )
    page_obj = Paginator(qs, 10).get_page(request.GET.get('page'))
    return render(request, 'admin_panel/hotel_bookings.html', {
        'active':   'hotel_bookings',
        'bookings': page_obj,
        'search':   search,
    })

def _set_booking_status(request, booking, model_class):
    """Update a booking's status, accepting only values the model defines."""
    new_status = request.POST.get('status', '')
    valid = {value for value, _label in model_class._meta.get_field('status').choices}
    if new_status not in valid:
        messages.error(request, 'Invalid booking status.')
        return False
    booking.status = new_status
    booking.save()
    messages.success(request, 'Booking status updated.')
    return True


@staff_required
def admin_update_hotel_booking(request, pk):
    b = get_object_or_404(HotelBooking, pk=pk)
    if request.method == 'POST':
        _set_booking_status(request, b, HotelBooking)
    return redirect('admin_hotel_bookings')

@staff_required
def admin_event_bookings(request):
    search = request.GET.get('q', '').strip()
    qs = EventBooking.objects.select_related('user', 'event').order_by('-created_at')
    if search:
        qs = qs.filter(
            models.Q(user__username__icontains=search) |
            models.Q(user__first_name__icontains=search) |
            models.Q(user__last_name__icontains=search) |
            models.Q(event__name__icontains=search)
        )
    page_obj = Paginator(qs, 10).get_page(request.GET.get('page'))
    return render(request, 'admin_panel/event_bookings.html', {
        'active':   'event_bookings',
        'bookings': page_obj,
        'search':   search,
    })

@staff_required
def admin_update_event_booking(request, pk):
    b = get_object_or_404(EventBooking, pk=pk)
    if request.method == 'POST':
        _set_booking_status(request, b, EventBooking)
    return redirect('admin_event_bookings')

@staff_required
def admin_service_bookings(request):
    search = request.GET.get('q', '').strip()
    qs = ServiceBooking.objects.select_related('user', 'service__attraction').order_by('-created_at')
    if search:
        qs = qs.filter(
            models.Q(user__username__icontains=search) |
            models.Q(user__first_name__icontains=search) |
            models.Q(user__last_name__icontains=search) |
            models.Q(service__attraction__name__icontains=search) |
            models.Q(service__name__icontains=search)
        )
    page_obj = Paginator(qs, 10).get_page(request.GET.get('page'))
    return render(request, 'admin_panel/service_bookings.html', {
        'active':   'service_bookings',
        'bookings': page_obj,
        'search':   search,
    })

@staff_required
def admin_update_service_booking(request, pk):
    b = get_object_or_404(ServiceBooking, pk=pk)
    if request.method == 'POST':
        _set_booking_status(request, b, ServiceBooking)
    return redirect('admin_service_bookings')


# ── Users ──
@staff_required
def admin_users(request):
    return render(request, 'admin_panel/users.html', {
        'active': 'users',
        'users': User.objects.order_by('-date_joined'),
    })


# ── Categories ──
@staff_required
def admin_categories(request):
    return render(request, 'admin_panel/categories.html', {
        'active': 'categories',
        'categories': Category.objects.all().order_by('name'),
    })

@staff_required
def admin_category_add(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if name:
            Category.objects.create(name=name)
            messages.success(request, f'Category "{name}" added.')
            return redirect('admin_categories')
    return render(request, 'admin_panel/category_form.html', {'active': 'categories'})

@staff_required
def admin_category_edit(request, pk):
    cat = get_object_or_404(Category, pk=pk)
    if request.method == 'POST':
        cat.name = request.POST.get('name', '').strip()
        cat.save()
        messages.success(request, f'Category "{cat.name}" updated.')
        return redirect('admin_categories')
    return render(request, 'admin_panel/category_form.html', {
        'active': 'categories',
        'category': cat,
    })

@staff_required
def admin_category_delete(request, pk):
    cat = get_object_or_404(Category, pk=pk)
    if request.method == 'POST':
        name = cat.name
        cat.delete()
        messages.success(request, f'Category "{name}" deleted.')
        return redirect('admin_categories')
    return render(request, 'admin_panel/confirm_delete.html', {
        'active': 'categories',
        'object_type': 'Category',
        'object_name': cat.name,
        'cancel_url': '/panel/categories/',
    })


# ── Attraction Services ──
@staff_required
def admin_services(request):
    return render(request, 'admin_panel/services.html', {
        'active': 'services',
        'services': AttractionService.objects.select_related('attraction').order_by('attraction__name', 'name'),
    })

@staff_required
def admin_service_add(request):
    if request.method == 'POST':
        s = AttractionService(
            attraction_id=request.POST.get('attraction'),
            name=request.POST.get('name'),
            description=request.POST.get('description', ''),
            service_type=request.POST.get('service_type', 'activity'),
            price=request.POST.get('price', 0),
            capacity=request.POST.get('capacity', 1),
            duration=request.POST.get('duration', ''),
            is_available='is_available' in request.POST,
        )
        s.save()
        messages.success(request, f'Service "{s.name}" added.')
        return redirect('admin_services')
    return render(request, 'admin_panel/service_form.html', {
        'active': 'services',
        'attractions': Attraction.objects.all().order_by('name'),
    })

@staff_required
def admin_service_edit(request, pk):
    s = get_object_or_404(AttractionService, pk=pk)
    if request.method == 'POST':
        s.attraction_id = request.POST.get('attraction')
        s.name         = request.POST.get('name')
        s.description  = request.POST.get('description', '')
        s.service_type = request.POST.get('service_type', 'activity')
        s.price        = request.POST.get('price', 0)
        s.capacity     = request.POST.get('capacity', 1)
        s.duration     = request.POST.get('duration', '')
        s.is_available = 'is_available' in request.POST
        s.save()
        messages.success(request, f'Service "{s.name}" updated.')
        return redirect('admin_services')
    return render(request, 'admin_panel/service_form.html', {
        'active': 'services',
        'service': s,
        'attractions': Attraction.objects.all().order_by('name'),
    })

@staff_required
def admin_service_delete(request, pk):
    s = get_object_or_404(AttractionService, pk=pk)
    if request.method == 'POST':
        name = s.name
        s.delete()
        messages.success(request, f'Service "{name}" deleted.')
        return redirect('admin_services')
    return render(request, 'admin_panel/confirm_delete.html', {
        'active': 'services',
        'object_type': 'Attraction Service',
        'object_name': s.name,
        'cancel_url': '/panel/services/',
    })


# ── Rooms ──
@staff_required
def admin_rooms(request):
    return render(request, 'admin_panel/rooms.html', {
        'active': 'rooms',
        'rooms': Room.objects.select_related('hotel').order_by('hotel__name', 'room_number'),
    })

@staff_required
def admin_room_add(request):
    if request.method == 'POST':
        r = Room(
            hotel_id=request.POST.get('hotel'),
            room_number=request.POST.get('room_number'),
            room_type=request.POST.get('room_type', 'single'),
            price=request.POST.get('price', 0),
            max_guests=request.POST.get('max_guests', 2),
            description=request.POST.get('description', ''),
            is_available='is_available' in request.POST,
        )
        if 'image' in request.FILES:
            r.image = request.FILES['image']
        r.save()
        messages.success(request, f'Room {r.room_number} added.')
        return redirect('admin_rooms')
    return render(request, 'admin_panel/room_form.html', {
        'active': 'rooms',
        'hotels': Hotel.objects.all().order_by('name'),
    })

@staff_required
def admin_room_edit(request, pk):
    r = get_object_or_404(Room, pk=pk)
    if request.method == 'POST':
        r.hotel_id    = request.POST.get('hotel')
        r.room_number = request.POST.get('room_number')
        r.room_type   = request.POST.get('room_type', 'single')
        r.price       = request.POST.get('price', 0)
        r.max_guests  = request.POST.get('max_guests', 2)
        r.description = request.POST.get('description', '')
        r.is_available = 'is_available' in request.POST
        if 'image' in request.FILES:
            r.image = request.FILES['image']
        r.save()
        messages.success(request, f'Room {r.room_number} updated.')
        return redirect('admin_rooms')
    return render(request, 'admin_panel/room_form.html', {
        'active': 'rooms',
        'room': r,
        'hotels': Hotel.objects.all().order_by('name'),
    })

@staff_required
def admin_room_delete(request, pk):
    r = get_object_or_404(Room, pk=pk)
    if request.method == 'POST':
        name = f'Room {r.room_number} ({r.hotel.name})'
        r.delete()
        messages.success(request, f'{name} deleted.')
        return redirect('admin_rooms')
    return render(request, 'admin_panel/confirm_delete.html', {
        'active': 'rooms',
        'object_type': 'Room',
        'object_name': f'Room {r.room_number} — {r.hotel.name}',
        'cancel_url': '/panel/rooms/',
    })


# ── Contacts ──
@staff_required
def admin_contacts(request):
    return render(request, 'admin_panel/contacts.html', {
        'active': 'contacts',
        'contacts': Contact.objects.order_by('-created_at'),
    })


# ── PAYMONGO PAYMENT ──────────────────────────────
import json
import base64
import urllib.request
import urllib.error
from decimal import Decimal
from django.conf import settings
from .models import Payment


def _paymongo_headers():
    key = settings.PAYMONGO_SECRET_KEY
    encoded = base64.b64encode(f'{key}:'.encode()).decode()
    return {
        'Authorization': f'Basic {encoded}',
        'Content-Type':  'application/json',
        'Accept':        'application/json',
    }


def _get_booking_description(data):
    btype = data.get('type')
    if btype == 'hotel':
        room = Room.objects.get(pk=data['room_id'])
        return f"Hotel Booking: {room.hotel.name} Room {room.room_number}"
    elif btype == 'event':
        event = Event.objects.get(pk=data['event_id'])
        return f"Event Ticket: {event.name} x{data['num_tickets']}"
    elif btype == 'service':
        service = AttractionService.objects.get(pk=data['service_id'])
        return f"Attraction Service: {service.attraction.name} - {service.name}"
    return "Iligan Tourism Booking"


@login_required
def payment_checkout(request):
    data = request.session.get('pending_booking')
    if not data:
        messages.error(request, 'No pending booking found.')
        return redirect('home')

    total_price  = Decimal(data['total_price'])
    if total_price <= 0:
        request.session.pop('pending_booking', None)
        messages.error(request, 'This booking has no payable amount, so it cannot be paid online. Please contact us.')
        return redirect('my_bookings')
    description  = _get_booking_description(data)
    amount_cents = int(total_price * 100)  # PayMongo uses centavos

    # PayMongo requires a valid email — use a fallback if user has none
    user_email = request.user.email or f'{request.user.username}@iligan-tourism.com'
    user_name  = request.user.get_full_name() or request.user.username

    payload = {
        "data": {
            "attributes": {
                "billing": {
                    "name":  user_name,
                    "email": user_email,
                    "phone": "",
                },
                "line_items": [
                    {
                        "currency":    "PHP",
                        "amount":      amount_cents,
                        "description": description,
                        "name":        description[:50],
                        "quantity":    1,
                    }
                ],
                "payment_method_types": ["gcash", "paymaya", "card", "grab_pay"],
                "success_url": f"{settings.SITE_URL}/payment/success/",
                "cancel_url":  f"{settings.SITE_URL}/payment/cancel/",
                "description": description,
            }
        }
    }

    try:
        req = urllib.request.Request(
            f"{settings.PAYMONGO_API_URL}/checkout_sessions",
            data=json.dumps(payload).encode(),
            headers=_paymongo_headers(),
            method='POST',
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())

        session_id   = result['data']['id']
        checkout_url = result['data']['attributes']['checkout_url']

        # Save payment record
        payment = Payment.objects.create(
            user=request.user,
            booking_type=data['type'],
            booking_data=data,
            amount=total_price,
            paymongo_session_id=session_id,
        )
        request.session['payment_id'] = payment.pk
        request.session.modified = True

        return redirect(checkout_url)

    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        import logging
        logging.error(f'PayMongo HTTPError: {e.code} — {error_body}')
        messages.error(request, 'We could not start the payment. Please try again in a moment.')
        return redirect('my_bookings')
    except Exception as e:
        import logging
        logging.error(f'PayMongo Exception: {str(e)}')
        messages.error(request, 'We could not start the payment. Please try again in a moment.')
        return redirect('my_bookings')


class BookingUnavailable(Exception):
    """Raised when a paid-for booking can no longer be fulfilled."""


def _create_booking_from_payment(payment, user):
    """
    Create the booking for a paid Payment and link it to the payment.
    Call inside transaction.atomic(). Availability is re-checked here because it
    may have changed while the customer was on the PayMongo page.
    """
    from datetime import date as date_type
    data = payment.booking_data

    if data['type'] == 'hotel':
        check_in  = date_type.fromisoformat(data['check_in'])
        check_out = date_type.fromisoformat(data['check_out'])
        if not Room.objects.filter(pk=data['room_id'], is_available=True).exists():
            raise BookingUnavailable('this room is no longer available.')
        if HotelBooking.objects.filter(
            room_id=data['room_id'], status__in=('confirmed', 'pending'),
            check_in__lt=check_out, check_out__gt=check_in,
        ).exists():
            raise BookingUnavailable('this room was booked by someone else for those dates.')
        ref = _generate_reference('HTL')
        while HotelBooking.objects.filter(reference_number=ref).exists():
            ref = _generate_reference('HTL')
        booking = HotelBooking.objects.create(
            user=user,
            room_id=data['room_id'],
            check_in=data['check_in'],
            check_in_time=data.get('check_in_time'),
            check_out=data['check_out'],
            check_out_time=data.get('check_out_time'),
            total_price=Decimal(data['total_price']),
            status='confirmed',
            reference_number=ref,
        )
        payment.hotel_booking = booking

    elif data['type'] == 'event':
        event = Event.objects.filter(pk=data['event_id']).first()
        if event is None:
            raise BookingUnavailable('this event is no longer available.')
        if event.ticket_capacity > 0:
            booked = EventBooking.objects.filter(
                event=event, status__in=('confirmed', 'pending')
            ).aggregate(total=models.Sum('num_tickets'))['total'] or 0
            if booked + data['num_tickets'] > event.ticket_capacity:
                raise BookingUnavailable('there are not enough tickets left for this event.')
        ref = _generate_reference('EVT')
        while EventBooking.objects.filter(reference_number=ref).exists():
            ref = _generate_reference('EVT')
        booking = EventBooking.objects.create(
            user=user,
            event_id=data['event_id'],
            num_tickets=data['num_tickets'],
            total_price=Decimal(data['total_price']),
            status='confirmed',
            reference_number=ref,
        )
        payment.event_booking = booking

    elif data['type'] == 'service':
        if not AttractionService.objects.filter(pk=data['service_id'], is_available=True).exists():
            raise BookingUnavailable('this service is no longer available.')
        ref = _generate_reference('SVC')
        while ServiceBooking.objects.filter(reference_number=ref).exists():
            ref = _generate_reference('SVC')
        booking = ServiceBooking.objects.create(
            user=user,
            service_id=data['service_id'],
            visit_date=data['visit_date'],
            num_guests=data['num_guests'],
            notes=data.get('notes', ''),
            total_price=Decimal(data['total_price']),
            status='confirmed',
            reference_number=ref,
        )
        payment.service_booking = booking


@login_required
def payment_success(request):
    payment_id = request.session.get('payment_id')
    if not payment_id:
        return redirect('my_bookings')

    payment = get_object_or_404(Payment, pk=payment_id, user=request.user)

    # Verify payment with PayMongo
    try:
        req = urllib.request.Request(
            f"{settings.PAYMONGO_API_URL}/checkout_sessions/{payment.paymongo_session_id}",
            headers=_paymongo_headers(),
            method='GET',
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())

        status = result['data']['attributes']['payment_intent']['attributes']['status']
        paid   = (status == 'succeeded')

    except Exception:
        # If verification fails, check if payment was already created
        paid = payment.status == 'paid'

    if paid and payment.status != 'paid':
        try:
            with transaction.atomic():
                # Re-read the payment inside the transaction so that two simultaneous
                # requests cannot both create a booking for the same payment.
                payment = Payment.objects.select_for_update().get(pk=payment.pk, user=request.user)
                if payment.status != 'paid':
                    _create_booking_from_payment(payment, request.user)
                    payment.status = 'paid'
                    payment.save()
        except BookingUnavailable as reason:
            # The customer HAS paid, but the room/tickets were taken while they were paying.
            # Record the payment so staff can see it in the admin and arrange a refund.
            Payment.objects.filter(pk=payment.pk).update(status='paid')
            logging.error(f'Paid but not fulfilled: payment {payment.pk} ({reason})')
            request.session.pop('pending_booking', None)
            request.session.pop('payment_id', None)
            messages.error(
                request,
                f'Your payment was received, but we could not complete your booking: {reason} '
                f'Please contact us with payment reference PAY-{payment.pk} so we can refund or rebook you.'
            )
            return redirect('my_bookings')
        # Clear session
        request.session.pop('pending_booking', None)
        request.session.pop('payment_id', None)

    return render(request, 'main/payment_success.html', {
        'payment': payment,
        'paid':    paid,
    })


@login_required
def payment_cancel(request):
    payment_id = request.session.get('payment_id')
    if payment_id:
        try:
            payment = Payment.objects.get(pk=payment_id, user=request.user)
            payment.status = 'failed'
            payment.save()
        except Payment.DoesNotExist:
            pass
    request.session.pop('pending_booking', None)
    request.session.pop('payment_id', None)
    messages.warning(request, 'Payment was cancelled. Your booking was not confirmed.')
    return render(request, 'main/payment_cancel.html')


# ── STAFF SYSTEM ──────────────────────────────────
from .models import Staff

# alias for admin check
staff_required_admin = staff_required

def staff_member_required(view_func):
    from functools import wraps
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not hasattr(request.user, 'staff_profile'):
            return redirect('home')
        return view_func(request, *args, **kwargs)
    return wrapper


def _get_staff_bookings(staff, status_filter=None):
    """Return bookings for the staff's assigned location as unified list."""
    bookings = []
    today = timezone.now().date()

    if staff.role == 'attraction' and staff.attraction:
        qs = ServiceBooking.objects.filter(
            service__attraction=staff.attraction
        ).select_related('user', 'service__attraction').order_by('-created_at')
        if status_filter:
            qs = qs.filter(status=status_filter)
        for b in qs:
            b.detail       = f"{b.service.name} x{b.num_guests}"
            b.date         = b.visit_date
            b.booking_type = 'service'
            b.is_today     = (b.visit_date == today)
            bookings.append(b)

    elif staff.role == 'hotel' and staff.hotel:
        qs = HotelBooking.objects.filter(
            room__hotel=staff.hotel
        ).select_related('user', 'room__hotel').order_by('-created_at')
        if status_filter:
            qs = qs.filter(status=status_filter)
        for b in qs:
            b.detail       = f"Room {b.room.room_number} ({b.room.get_room_type_display()})"
            b.date         = b.check_in
            b.booking_type = 'hotel'
            b.is_today     = (b.check_in == today)
            bookings.append(b)

    elif staff.role == 'event' and staff.event:
        qs = EventBooking.objects.filter(
            event=staff.event
        ).select_related('user', 'event').order_by('-created_at')
        if status_filter:
            qs = qs.filter(status=status_filter)
        for b in qs:
            b.detail       = f"{b.num_tickets} ticket(s)"
            b.date         = b.event.date
            b.booking_type = 'event'
            b.is_today     = (b.event.date == today)
            bookings.append(b)

    return bookings


@staff_member_required
def staff_dashboard(request):
    staff   = request.user.staff_profile
    today   = timezone.now().date()
    all_b   = _get_staff_bookings(staff)
    today_b = [b for b in all_b if b.is_today]
    return render(request, 'staff/dashboard.html', {
        'active':          'dashboard',
        'today_bookings':  today_b,
        'today_count':     len(today_b),
        'confirmed_count': sum(1 for b in all_b if b.status == 'confirmed'),
        'completed_count': sum(1 for b in all_b if b.status == 'completed'),
    })


@staff_member_required
def staff_bookings(request):
    staff         = request.user.staff_profile
    status_filter = request.GET.get('status', '')
    search        = request.GET.get('q', '').strip()
    bookings      = _get_staff_bookings(staff, status_filter or None)

    # Search by customer username or reference number
    if search:
        bookings = [b for b in bookings if
            search.lower() in b.user.username.lower() or
            search.lower() in (b.user.get_full_name() or '').lower() or
            search.lower() in (b.reference_number or '').lower()]

    paginator   = Paginator(bookings, 10)
    page_obj    = paginator.get_page(request.GET.get('page'))
    return render(request, 'staff/bookings.html', {
        'active':        'bookings',
        'bookings':      page_obj,
        'status_filter': status_filter,
        'search':        search,
    })


def _staff_manages(staff, booking_type, booking):
    """True only if the booking belongs to the staff member's assigned location."""
    if staff.role == 'attraction' and staff.attraction_id:
        return booking_type == 'service' and booking.service.attraction_id == staff.attraction_id
    if staff.role == 'hotel' and staff.hotel_id:
        return booking_type == 'hotel' and booking.room.hotel_id == staff.hotel_id
    if staff.role == 'event' and staff.event_id:
        return booking_type == 'event' and booking.event_id == staff.event_id
    return False


@staff_member_required
def staff_complete_booking(request, booking_type, pk):
    if request.method == 'POST':
        if booking_type == 'hotel':
            b = get_object_or_404(HotelBooking, pk=pk)
        elif booking_type == 'event':
            b = get_object_or_404(EventBooking, pk=pk)
        elif booking_type == 'service':
            b = get_object_or_404(ServiceBooking, pk=pk)
        else:
            return redirect('staff_dashboard')
        if not _staff_manages(request.user.staff_profile, booking_type, b):
            messages.error(request, 'You can only manage bookings for your assigned location.')
            return redirect('staff_bookings')
        if b.status == 'confirmed':
            b.status = 'completed'
            b.save()
            messages.success(request, 'Booking marked as completed.')
    return redirect('staff_bookings')


# ── Admin Staff Management ──
@staff_required_admin
def admin_staff(request):
    return render(request, 'admin_panel/staff.html', {
        'active':     'staff',
        'staff_list': Staff.objects.select_related('user', 'attraction', 'hotel', 'event').order_by('-created_at'),
    })


@staff_required_admin
def admin_staff_add(request):
    if request.method == 'POST':
        username   = request.POST.get('username')
        password   = request.POST.get('password')
        first_name = request.POST.get('first_name', '')
        last_name  = request.POST.get('last_name', '')
        role       = request.POST.get('role')

        if User.objects.filter(username=username).exists():
            messages.error(request, f'Username "{username}" already exists.')
            return redirect('admin_staff_add')

        user = User.objects.create_user(
            username=username,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )

        staff = Staff(user=user, role=role)
        if role == 'attraction':
            att_id = request.POST.get('attraction')
            if att_id:
                staff.attraction_id = att_id
        elif role == 'hotel':
            hotel_id = request.POST.get('hotel')
            if hotel_id:
                staff.hotel_id = hotel_id
        elif role == 'event':
            event_id = request.POST.get('event')
            if event_id:
                staff.event_id = event_id
        staff.save()

        messages.success(request, f'Staff account "{username}" created.')
        return redirect('admin_staff')

    return render(request, 'admin_panel/staff_form.html', {
        'active':      'staff',
        'attractions': Attraction.objects.all().order_by('name'),
        'hotels':      Hotel.objects.all().order_by('name'),
        'events':      Event.objects.all().order_by('name'),
    })


@staff_required_admin
def admin_staff_delete(request, pk):
    s = get_object_or_404(Staff, pk=pk)
    if request.method == 'POST':
        username = s.user.username
        s.user.delete()  # deletes staff profile too via cascade
        messages.success(request, f'Staff account "{username}" deleted.')
        return redirect('admin_staff')
    return render(request, 'admin_panel/confirm_delete.html', {
        'active':      'staff',
        'object_type': 'Staff Account',
        'object_name': s.user.username,
        'cancel_url':  '/panel/staff/',
    })


# ── ADMIN BOOKING DETAIL VIEWS ────────────────────

@staff_required
def admin_hotel_booking_detail(request, pk):
    booking = get_object_or_404(HotelBooking, pk=pk)
    return render(request, 'admin_panel/booking_detail.html', {
        'active':       'hotel_bookings',
        'booking':      booking,
        'booking_type': 'hotel',
        'back_url':     '/panel/bookings/hotels/',
        'confirm_url':  f'/panel/bookings/hotels/{pk}/update/',
    })

@staff_required
def admin_event_booking_detail(request, pk):
    booking = get_object_or_404(EventBooking, pk=pk)
    return render(request, 'admin_panel/booking_detail.html', {
        'active':       'event_bookings',
        'booking':      booking,
        'booking_type': 'event',
        'back_url':     '/panel/bookings/events/',
        'confirm_url':  f'/panel/bookings/events/{pk}/update/',
    })

@staff_required
def admin_service_booking_detail(request, pk):
    booking = get_object_or_404(ServiceBooking, pk=pk)
    return render(request, 'admin_panel/booking_detail.html', {
        'active':       'service_bookings',
        'booking':      booking,
        'booking_type': 'service',
        'back_url':     '/panel/bookings/services/',
        'confirm_url':  f'/panel/bookings/services/{pk}/update/',
    })


# ── STAFF BOOKING DETAIL VIEW ─────────────────────

@staff_member_required
def staff_booking_detail(request, booking_type, pk):
    if booking_type == 'hotel':
        booking = get_object_or_404(HotelBooking, pk=pk)
    elif booking_type == 'event':
        booking = get_object_or_404(EventBooking, pk=pk)
    elif booking_type == 'service':
        booking = get_object_or_404(ServiceBooking, pk=pk)
    else:
        return redirect('staff_bookings')
    return render(request, 'staff/booking_detail.html', {
        'active':       'bookings',
        'booking':      booking,
        'booking_type': booking_type,
    })