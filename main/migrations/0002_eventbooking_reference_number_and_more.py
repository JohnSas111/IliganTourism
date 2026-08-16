from django.db import migrations, models
import random, string

def generate_ref(prefix):
    chars = string.ascii_uppercase + string.digits
    return prefix + '-' + ''.join(random.choices(chars, k=8))

def populate_refs(apps, schema_editor):
    for Model, prefix in [
        (apps.get_model('main', 'EventBooking'),   'EVT'),
        (apps.get_model('main', 'HotelBooking'),   'HTL'),
        (apps.get_model('main', 'ServiceBooking'), 'SVC'),
    ]:
        for obj in Model.objects.all():
            while True:
                ref = generate_ref(prefix)
                if not Model.objects.filter(reference_number=ref).exists():
                    obj.reference_number = ref
                    obj.save(update_fields=['reference_number'])
                    break

class Migration(migrations.Migration):

    dependencies = [
        ('main', '0001_initial'),
    ]

    operations = [
        # Add nullable first so existing rows don't conflict
        migrations.AddField(
            model_name='eventbooking',
            name='reference_number',
            field=models.CharField(blank=True, max_length=20, default=''),
        ),
        migrations.AddField(
            model_name='hotelbooking',
            name='reference_number',
            field=models.CharField(blank=True, max_length=20, default=''),
        ),
        migrations.AddField(
            model_name='servicebooking',
            name='reference_number',
            field=models.CharField(blank=True, max_length=20, default=''),
        ),
        # Populate existing rows
        migrations.RunPython(populate_refs, migrations.RunPython.noop),
        # Now add unique constraint
        migrations.AlterField(
            model_name='eventbooking',
            name='reference_number',
            field=models.CharField(blank=True, max_length=20, unique=True),
        ),
        migrations.AlterField(
            model_name='hotelbooking',
            name='reference_number',
            field=models.CharField(blank=True, max_length=20, unique=True),
        ),
        migrations.AlterField(
            model_name='servicebooking',
            name='reference_number',
            field=models.CharField(blank=True, max_length=20, unique=True),
        ),
    ]
