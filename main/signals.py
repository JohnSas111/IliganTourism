"""
Keeps Cloudinary tidy (Batch 5): when a picture is replaced, or its record is deleted,
the old Cloudinary file is deleted too, so the free storage is not filled with unused files.
Local files (old images in media/) are never touched, exactly like before.
"""
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save

from .cloud_storage import is_cloud_name
from .models import Attraction, Event, Hotel, Room

IMAGE_MODELS = (Attraction, Hotel, Event, Room)


def _delete_later(storage, name):
    # Only after the database change is final, so a rolled-back change never loses a picture.
    transaction.on_commit(lambda: storage.delete(name))


def remember_old_image(sender, instance, raw=False, **kwargs):
    if raw:
        return
    old = ''
    if instance.pk:
        old = sender.objects.filter(pk=instance.pk).values_list('image', flat=True).first() or ''
    instance._old_image_name = old


def delete_replaced_image(sender, instance, created=False, raw=False, **kwargs):
    if raw:
        return
    old = getattr(instance, '_old_image_name', '')
    instance._old_image_name = instance.image.name or ''
    if is_cloud_name(old) and old != (instance.image.name or ''):
        _delete_later(instance.image.storage, old)


def delete_image_of_deleted_record(sender, instance, **kwargs):
    name = instance.image.name or ''
    if is_cloud_name(name):
        _delete_later(instance.image.storage, name)


for _model in IMAGE_MODELS:
    pre_save.connect(remember_old_image, sender=_model, dispatch_uid=f'img_pre_{_model.__name__}')
    post_save.connect(delete_replaced_image, sender=_model, dispatch_uid=f'img_post_{_model.__name__}')
    post_delete.connect(delete_image_of_deleted_record, sender=_model, dispatch_uid=f'img_del_{_model.__name__}')
