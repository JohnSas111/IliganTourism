HOW TO ADD YOUR IMAGES
======================

1. Download your images and put them in this folder (static/images/)

2. Open templates/main/home.html and find the image boxes like:
      <div class="card-img-box">Image Here</div>

   Replace with:
      <div class="card-img-box">
        <img src="{% static 'images/YOUR-IMAGE-NAME.jpg' %}" alt="Description">
      </div>

3. Make sure the top of the template has:
      {% load static %}

SUGGESTED FILE NAMES:
---------------------
hero-bg.jpg          → Hero section background
logo.png             → Navbar logo (36x36px)
maria-cristina.jpg   → Maria Cristina Falls card
tinago.jpg           → Tinago Falls card
mimbalut.jpg         → Mimbalut Falls card
timoga.jpg           → Timoga Spring Resort card
cathedral.jpg        → Saint Michael's Cathedral card
museum.jpg           → Iligan City Museum card
hotel-1.jpg          → Hotel Iligan City card
hotel-2.jpg          → Maria Cristina Hotel card
hotel-3.jpg          → Waterfall Inn card
event-1.jpg          → Diyandi Festival card
event-2.jpg          → Waterfall Trekking card
event-3.jpg          → Iligan Iron Duathlon card
