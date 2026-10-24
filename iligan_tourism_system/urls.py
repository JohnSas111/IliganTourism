from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views
from main import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.home, name='home'),

    # Attractions
    path('attractions/',                           views.attractions_list,  name='attractions'),
    path('attractions/<int:pk>/',                  views.attraction_detail, name='attraction_detail'),
    path('attractions/book/<int:service_id>/',     views.book_service,      name='book_service'),

    # Events
    path('events/',                                views.events_list,       name='events'),
    path('events/<int:pk>/',                       views.event_detail,      name='event_detail'),
    path('events/book/<int:event_id>/',            views.book_event,        name='book_event'),

    # Hotels
    path('hotels/',                                views.hotels_list,       name='hotels'),
    path('hotels/<int:pk>/',                       views.hotel_detail,      name='hotel_detail'),
    path('hotels/room/<int:pk>/',                  views.room_detail,       name='room_detail'),
    path('hotels/room/<int:room_id>/book/',        views.book_room,         name='book_room'),

    # My Bookings
    path('my-bookings/',                           views.my_bookings,            name='my_bookings'),
    path('my-bookings/<str:booking_type>/<int:pk>/', views.booking_detail,        name='booking_detail'),
    path('my-bookings/hotel/<int:pk>/cancel/',     views.cancel_hotel_booking,   name='cancel_hotel_booking'),
    path('my-bookings/event/<int:pk>/cancel/',     views.cancel_event_booking,   name='cancel_event_booking'),
    path('my-bookings/service/<int:pk>/cancel/',   views.cancel_service_booking, name='cancel_service_booking'),

    # Contact
    path('contact/',                               views.contact,           name='contact'),
    path('api/weather/<str:kind>/<int:pk>/',       views.weather_api,       name='weather_api'),
    path('assistant/ask/',                         views.assistant_ask,     name='assistant_ask'),

    # Payment
    path('payment/checkout/',                      views.payment_checkout,  name='payment_checkout'),
    path('payment/success/',                       views.payment_success,   name='payment_success'),
    path('payment/cancel/',                        views.payment_cancel,    name='payment_cancel'),

    # Auth
    path('login/',                                 views.custom_login,      name='login'),
    path('logout/',                                auth_views.LogoutView.as_view(), name='logout'),
    path('register/',                              views.register,          name='register'),
    path('account/',                               views.account_settings,  name='account_settings'),

    # Custom Admin Panel
    path('panel/',                                 views.admin_dashboard,              name='admin_dashboard'),
    path('panel/attractions/',                     views.admin_attractions,            name='admin_attractions'),
    path('panel/attractions/add/',                 views.admin_attraction_add,         name='admin_attraction_add'),
    path('panel/attractions/<int:pk>/edit/',       views.admin_attraction_edit,        name='admin_attraction_edit'),
    path('panel/attractions/<int:pk>/delete/',     views.admin_attraction_delete,      name='admin_attraction_delete'),
    path('panel/hotels/',                          views.admin_hotels,                 name='admin_hotels'),
    path('panel/hotels/add/',                      views.admin_hotel_add,              name='admin_hotel_add'),
    path('panel/hotels/<int:pk>/edit/',            views.admin_hotel_edit,             name='admin_hotel_edit'),
    path('panel/hotels/<int:pk>/delete/',          views.admin_hotel_delete,           name='admin_hotel_delete'),
    path('panel/events/',                          views.admin_events,                 name='admin_events'),
    path('panel/events/add/',                      views.admin_event_add,              name='admin_event_add'),
    path('panel/events/<int:pk>/edit/',            views.admin_event_edit,             name='admin_event_edit'),
    path('panel/events/<int:pk>/delete/',          views.admin_event_delete,           name='admin_event_delete'),
    path('panel/bookings/hotels/',                 views.admin_hotel_bookings,         name='admin_hotel_bookings'),
    path('panel/bookings/hotels/<int:pk>/update/', views.admin_update_hotel_booking,   name='admin_update_hotel_booking'),
    path('panel/bookings/events/',                 views.admin_event_bookings,         name='admin_event_bookings'),
    path('panel/bookings/events/<int:pk>/update/', views.admin_update_event_booking,   name='admin_update_event_booking'),
    path('panel/bookings/services/',               views.admin_service_bookings,       name='admin_service_bookings'),
    path('panel/bookings/services/<int:pk>/update/', views.admin_update_service_booking, name='admin_update_service_booking'),
    path('panel/users/',                           views.admin_users,                  name='admin_users'),

    # Categories
    path('panel/categories/',                      views.admin_categories,             name='admin_categories'),
    path('panel/categories/add/',                  views.admin_category_add,           name='admin_category_add'),
    path('panel/categories/<int:pk>/edit/',        views.admin_category_edit,          name='admin_category_edit'),
    path('panel/categories/<int:pk>/delete/',      views.admin_category_delete,        name='admin_category_delete'),

    # Attraction Services
    path('panel/services/',                        views.admin_services,               name='admin_services'),
    path('panel/services/add/',                    views.admin_service_add,            name='admin_service_add'),
    path('panel/services/<int:pk>/edit/',          views.admin_service_edit,           name='admin_service_edit'),
    path('panel/services/<int:pk>/delete/',        views.admin_service_delete,         name='admin_service_delete'),

    # Rooms
    path('panel/rooms/',                           views.admin_rooms,                  name='admin_rooms'),
    path('panel/rooms/add/',                       views.admin_room_add,               name='admin_room_add'),
    path('panel/rooms/<int:pk>/edit/',             views.admin_room_edit,              name='admin_room_edit'),
    path('panel/rooms/<int:pk>/delete/',           views.admin_room_delete,            name='admin_room_delete'),

    # Contacts
    path('panel/contacts/',                        views.admin_contacts,               name='admin_contacts'),

    # Staff Management (admin)
    path('panel/staff/',                           views.admin_staff,                  name='admin_staff'),
    path('panel/staff/add/',                       views.admin_staff_add,              name='admin_staff_add'),
    path('panel/staff/<int:pk>/delete/',           views.admin_staff_delete,           name='admin_staff_delete'),

    # Staff Dashboard
    path('staff/',                                 views.staff_dashboard,              name='staff_dashboard'),
    path('staff/bookings/',                        views.staff_bookings,               name='staff_bookings'),
    path('staff/bookings/<str:booking_type>/<int:pk>/complete/', views.staff_complete_booking, name='staff_complete_booking'),


] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)