from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import CommunityPost


@receiver(post_delete, sender=CommunityPost)
def delete_post_photo_after_commit(sender, instance, **kwargs):
    if instance.image:
        storage = instance.image.storage
        name = instance.image.name
        transaction.on_commit(lambda: storage.delete(name))
