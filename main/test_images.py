"""
Batch 5 tests: tourism images and Cloudinary.

NOTHING here talks to the internet. Cloudinary's upload and delete calls are replaced by
simulated ones, and the real Cloudinary library is tested with its network layer replaced.

Run only these:   py manage.py test main.test_images
"""
import hashlib
import importlib.util
import io
import json
import os
import tempfile
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from PIL import Image

from . import cloud_storage
from .cloud_storage import (
    CLOUD_PREFIX, HybridMediaStorage, ImageStorageError, ImageValidationError,
    cloudinary_status, delivery_url, validate_image_upload,
)
from .models import Attraction, Event, Hotel, Room
from .tests import BaseCase, PASSWORD

API_KEY = '123456789012345'
API_SECRET = 'SECRET-abcdefghijklmnopqrstuvwxyz'
CLOUD = dict(
    CLOUDINARY_CLOUD_NAME='demo-cloud', CLOUDINARY_API_KEY=API_KEY, CLOUDINARY_API_SECRET=API_SECRET,
    CLOUDINARY_FOLDER='iligan_tourism', CLOUDINARY_DELIVERY_TRANSFORM='c_limit,w_1600,f_auto,q_auto',
    CLOUDINARY_TIMEOUT=30, IMAGE_MAX_UPLOAD_MB=8, IMAGE_MAX_MEGAPIXELS=25,
)
NO_CLOUD = dict(CLOUDINARY_CLOUD_NAME='', CLOUDINARY_API_KEY='', CLOUDINARY_API_SECRET='')


def no_proxy_environment():
    """Context that hides any HTTPS_PROXY variable of the machine running the tests."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
        os.environ.pop(key, None)
    return patcher


def picture(fmt='PNG', size=(40, 30)):
    buffer = io.BytesIO()
    Image.new('RGB', size, (200, 80, 40)).save(buffer, fmt)
    return buffer.getvalue()


def upload(name='photo.png', fmt='PNG', content=None):
    return SimpleUploadedFile(name, content if content is not None else picture(fmt))


class FakeCloudinary:
    """Stands in for cloudinary.uploader.upload / destroy and records every call."""

    def __init__(self, upload_error=None, destroy_error=None):
        self.uploads, self.destroys = [], []
        self.upload_error, self.destroy_error = upload_error, destroy_error

    def upload(self, file, **options):
        if self.upload_error:
            raise self.upload_error
        self.uploads.append(dict(options, _bytes=file.read()))
        return {'public_id': options['public_id'], 'version': 1, 'resource_type': 'image'}

    def destroy(self, public_id, **options):
        if self.destroy_error:
            raise self.destroy_error
        self.destroys.append(public_id)
        return {'result': 'ok'}

    def __enter__(self):
        self._env = no_proxy_environment()
        self._patches = [
            mock.patch('cloudinary.uploader.upload', side_effect=self.upload),
            mock.patch('cloudinary.uploader.destroy', side_effect=self.destroy),
        ]
        for patcher in self._patches:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in self._patches:
            patcher.stop()
        self._env.stop()


# ── 1. Validation ─────────────────────────────────────────────────────────

class ValidationTests(SimpleTestCase):

    def test_real_pictures_of_every_allowed_type_pass(self):
        for name, fmt in (('a.jpg', 'JPEG'), ('a.jpeg', 'JPEG'), ('a.png', 'PNG'),
                          ('a.webp', 'WEBP'), ('a.gif', 'GIF'), ('A.JPG', 'JPEG')):
            with self.subTest(name):
                validate_image_upload(upload(name, fmt))

    def test_text_pretending_to_be_a_picture_is_refused(self):
        with self.assertRaisesMessage(ImageValidationError, 'not a readable picture'):
            validate_image_upload(upload('evil.jpg', content=b'<?php echo 1; ?>'))

    def test_svg_and_other_types_are_refused(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        for name in ('x.svg', 'x.bmp', 'x.pdf', 'x.html', 'noextension'):
            with self.subTest(name), self.assertRaisesMessage(ImageValidationError, 'JPG, PNG, WebP or GIF'):
                validate_image_upload(upload(name, content=svg if name.endswith('svg') else picture()))

    def test_a_real_picture_of_a_forbidden_format_with_an_allowed_name_is_refused(self):
        with self.assertRaisesMessage(ImageValidationError, 'is not a JPG, PNG, WebP or GIF picture'):
            validate_image_upload(upload('sneaky.jpg', 'BMP'))

    def test_too_big_file_is_refused_with_the_limit_in_the_message(self):
        with override_settings(IMAGE_MAX_UPLOAD_MB=0):
            with self.assertRaisesMessage(ImageValidationError, 'The limit is 0 MB'):
                validate_image_upload(upload())

    def test_file_exactly_at_the_limit_is_accepted(self):
        data = picture()
        with override_settings(IMAGE_MAX_UPLOAD_MB=1):
            padded = data + b'\0' * (1024 * 1024 - len(data))     # trailing bytes are ignored by decoders
            self.assertEqual(len(padded), 1024 * 1024)
            validate_image_upload(upload('exact.png', content=padded))
            with self.assertRaises(ImageValidationError):
                validate_image_upload(upload('over.png', content=padded + b'\0'))

    def test_empty_file_is_refused(self):
        with self.assertRaisesMessage(ImageValidationError, 'is empty'):
            validate_image_upload(upload('empty.png', content=b''))

    def test_too_many_megapixels_is_refused(self):
        with override_settings(IMAGE_MAX_MEGAPIXELS=0):
            with self.assertRaisesMessage(ImageValidationError, 'megapixels'):
                validate_image_upload(upload())
        with override_settings(IMAGE_MAX_MEGAPIXELS=1):
            validate_image_upload(upload(content=picture(size=(1000, 1000))))          # exactly 1 MP
            with self.assertRaises(ImageValidationError):
                validate_image_upload(upload(content=picture(size=(1001, 1000))))

    def test_file_position_is_rewound_so_the_upload_still_works(self):
        item = upload()
        validate_image_upload(item)
        self.assertEqual(item.read(), picture())


# ── 2. The storage class (Cloudinary calls simulated) ────────────────────

@override_settings(**CLOUD)
class StorageTests(SimpleTestCase):

    def save(self, name='attractions/My Photo.PNG', data=None):
        return HybridMediaStorage().save(name, ContentFile(data or picture(), name='x.png'))

    def test_new_images_go_to_cloudinary_with_the_expected_options(self):
        with FakeCloudinary() as fake:
            stored = self.save()
        self.assertRegex(stored, r'^cld/iligan_tourism/attractions/My-Photo_[0-9a-f]{8}$')
        sent = fake.uploads[0]
        self.assertEqual(sent['public_id'], stored[len(CLOUD_PREFIX):])
        self.assertEqual(sent['resource_type'], 'image')
        self.assertIs(sent['overwrite'], False)            # a picture is never silently replaced
        self.assertEqual(sent['allowed_formats'], ['jpg', 'png', 'webp', 'gif'])
        self.assertEqual(sent['timeout'], 30)
        self.assertEqual((sent['cloud_name'], sent['api_key'], sent['api_secret']),
                         ('demo-cloud', API_KEY, API_SECRET))
        self.assertEqual(sent['_bytes'], picture())

    def test_every_upload_gets_a_different_name(self):
        with FakeCloudinary():
            self.assertNotEqual(self.save(), self.save())

    def test_awkward_file_names_become_safe_short_ids(self):
        awkward = ['attractions/a..b..c.png', 'attractions/%2e%2e/x.png', 'attractions/Ünï côdé 写真!.png',
                   'attractions/' + 'long' * 60 + '.png', 'attractions/   .png', 'a\\b\\c.png']
        with FakeCloudinary():
            for name in awkward:
                with self.subTest(name):
                    stored = self.save(name)
                    self.assertRegex(stored[len(CLOUD_PREFIX):], r'^[A-Za-z0-9_/-]+$')
                    self.assertNotIn('..', stored)
                    self.assertLessEqual(len(stored), 100)     # fits the database column

    def test_url_of_a_cloudinary_image(self):
        url = HybridMediaStorage().url('cld/iligan_tourism/hotels/x_1a2b3c4d')
        self.assertEqual(
            url, 'https://res.cloudinary.com/demo-cloud/image/upload/'
                 'c_limit,w_1600,f_auto,q_auto/iligan_tourism/hotels/x_1a2b3c4d')

    @override_settings(CLOUDINARY_DELIVERY_TRANSFORM='')
    def test_url_without_a_transformation_has_no_double_slash(self):
        self.assertEqual(delivery_url('cld/a/b_1'), 'https://res.cloudinary.com/demo-cloud/image/upload/a/b_1')

    @override_settings(CLOUDINARY_CLOUD_NAME='')
    def test_url_is_empty_not_a_crash_when_the_cloud_name_is_missing(self):
        self.assertEqual(HybridMediaStorage().url('cld/a/b_1'), '')

    def test_old_local_images_keep_their_media_url(self):
        self.assertEqual(HybridMediaStorage().url('attractions/timoga.png'), '/media/attractions/timoga.png')

    def test_status_levels(self):
        self.assertEqual(cloudinary_status(), 'ready')
        with override_settings(CLOUDINARY_API_SECRET=''):
            self.assertEqual(cloudinary_status(), 'partial')
        with override_settings(**NO_CLOUD):
            self.assertEqual(cloudinary_status(), 'off')

    def test_without_cloudinary_settings_images_are_saved_locally_like_before(self):
        for blank in (NO_CLOUD, dict(NO_CLOUD, CLOUDINARY_CLOUD_NAME='demo-cloud')):    # off, then partial
            with self.subTest(blank), tempfile.TemporaryDirectory() as folder, \
                    override_settings(MEDIA_ROOT=folder, **blank), FakeCloudinary() as fake:
                stored = HybridMediaStorage().save('attractions/pic.png', ContentFile(picture()))
                self.assertFalse(stored.startswith(CLOUD_PREFIX))
                self.assertTrue((Path(folder) / stored).is_file())
                self.assertEqual(fake.uploads, [])

    def test_upload_problems_become_friendly_messages(self):
        from cloudinary.exceptions import Error, RateLimited
        cases = [
            (Error('Invalid Signature abc. String to sign - x'), 'did not accept the account details'),
            (Error('Unknown API key 123'), 'did not accept the account details'),
            (ValueError('Must supply api_key'), 'did not accept the account details'),
            (Error('File size too large. Got 11275941. Maximum is 10485760.'), 'too large'),
            (RateLimited('Rate Limit Exceeded'), 'rate limit'),
            (Error("Socket error: OSError('Tunnel connection failed: 403 Forbidden')"), 'Could not reach Cloudinary'),
            (Error('Unexpected error - MaxRetryError'), 'Could not reach Cloudinary'),
            (Error('something strange'), 'could not store the picture'),
        ]
        for error, expected in cases:
            with self.subTest(str(error)), FakeCloudinary(upload_error=error), \
                    self.assertLogs('main.cloud_storage', 'ERROR'), \
                    self.assertRaisesMessage(ImageStorageError, expected):
                self.save()

    def test_the_key_and_secret_never_reach_the_message_or_the_log(self):
        from cloudinary.exceptions import Error
        leaky = Error(f'Invalid api_key {API_KEY}; secret {API_SECRET}')
        with FakeCloudinary(upload_error=leaky), self.assertLogs('main.cloud_storage', 'ERROR') as logs:
            with self.assertRaises(ImageStorageError) as caught:
                self.save()
        shown = str(caught.exception) + '\n'.join(logs.output)
        self.assertNotIn(API_SECRET, shown)
        self.assertNotIn(API_KEY, shown)
        self.assertIn('[hidden]', shown)

    def test_deleting_a_cloudinary_image_asks_cloudinary_to_remove_it(self):
        with FakeCloudinary() as fake:
            HybridMediaStorage().delete('cld/iligan_tourism/rooms/r_1a2b3c4d')
        self.assertEqual(fake.destroys, ['iligan_tourism/rooms/r_1a2b3c4d'])

    def test_a_failed_delete_is_logged_but_never_raises(self):
        with FakeCloudinary(destroy_error=OSError('network down')), \
                self.assertLogs('main.cloud_storage', 'WARNING') as logs:
            HybridMediaStorage().delete('cld/x/y_1')
        self.assertIn('Could not delete', logs.output[0])

    def test_deleting_a_local_image_removes_the_file_and_never_calls_cloudinary(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder), FakeCloudinary() as fake:
            target = Path(folder) / 'attractions' / 'old.png'
            target.parent.mkdir()
            target.write_bytes(picture())
            HybridMediaStorage().delete('attractions/old.png')
            self.assertFalse(target.exists())
            self.assertEqual(fake.destroys, [])

    @override_settings(**NO_CLOUD)
    def test_deleting_a_cloud_image_without_settings_does_nothing_and_does_not_crash(self):
        with FakeCloudinary() as fake, self.assertLogs('main.cloud_storage', 'WARNING'):
            HybridMediaStorage().delete('cld/x/y_1')
        self.assertEqual(fake.destroys, [])

    def test_cloud_images_have_no_local_file(self):
        storage = HybridMediaStorage()
        self.assertTrue(storage.exists('cld/x/y_1'))
        self.assertEqual(storage.size('cld/x/y_1'), 0)
        with self.assertRaises(NotImplementedError):
            storage.path('cld/x/y_1')
        with self.assertRaises(FileNotFoundError):
            storage.open('cld/x/y_1')

    def test_the_project_really_uses_this_storage(self):
        self.assertIsInstance(default_storage._wrapped, HybridMediaStorage)
        self.assertEqual(Attraction._meta.get_field('image').storage._wrapped.__class__, HybridMediaStorage)


# ── 3. The real Cloudinary library, with its network layer replaced ───────

@override_settings(**CLOUD)
class RealLibraryRequestTests(SimpleTestCase):
    """Runs the real SDK code (building, signing) and inspects what it WOULD send."""

    def setUp(self):
        env = no_proxy_environment()
        self.addCleanup(env.stop)

    def test_the_request_is_signed_and_the_secret_is_never_sent(self):
        import cloudinary.uploader
        sent = {}

        class Reply:
            status = 200
            headers = {'x-request-id': 'abc'}
            data = json.dumps({'public_id': 'will-be-overwritten', 'version': 7}).encode()

        def fake_request(method, url, fields, headers, **kw):
            sent.update(method=method, url=url, fields=list(fields), headers=headers, kw=kw)
            return Reply()

        with mock.patch.object(cloudinary.uploader._http, 'request', side_effect=fake_request):
            storage = HybridMediaStorage()
            stored = storage.save('attractions/pic.png', ContentFile(picture(), name='pic.png'))

        self.assertEqual(sent['method'], 'POST')
        self.assertEqual(sent['url'], 'https://api.cloudinary.com/v1_1/demo-cloud/image/upload')
        self.assertEqual(sent['kw'].get('timeout'), 30)
        fields = dict((k, v) for k, v in sent['fields'])
        self.assertEqual(fields['api_key'], API_KEY)
        self.assertTrue(fields['public_id'].startswith('iligan_tourism/attractions/pic_'))
        self.assertEqual(fields['allowed_formats'], 'jpg,png,webp,gif')
        self.assertIn('file', fields)
        # Independent re-computation of Cloudinary's signature scheme (SHA-1 over the sorted
        # parameters followed by the secret) proves the request is correctly signed.
        signed = {k: v for k, v in fields.items() if k not in ('file', 'api_key', 'signature', 'resource_type', 'cloud_name')}
        to_sign = '&'.join(f'{k}={v}' for k, v in sorted(signed.items()))
        self.assertEqual(fields['signature'], hashlib.sha1((to_sign + API_SECRET).encode()).hexdigest())
        # The secret itself must appear nowhere in what is sent.
        self.assertNotIn(API_SECRET, repr(sent['fields']) + repr(sent['headers']) + sent['url'])
        self.assertTrue(stored.startswith(CLOUD_PREFIX))

    def test_a_cloudinary_error_reply_becomes_a_friendly_message(self):
        import cloudinary.uploader

        class Reply:
            status = 401
            headers = {}
            data = json.dumps({'error': {'message': 'Invalid Signature xyz'}}).encode()

        with mock.patch.object(cloudinary.uploader._http, 'request', return_value=Reply()), \
                self.assertLogs('main.cloud_storage', 'ERROR'), \
                self.assertRaisesMessage(ImageStorageError, 'did not accept the account details'):
            HybridMediaStorage().save('hotels/x.png', ContentFile(picture(), name='x.png'))

    def test_a_network_failure_becomes_a_friendly_message(self):
        import cloudinary.uploader
        from urllib3.exceptions import MaxRetryError
        with mock.patch.object(cloudinary.uploader._http, 'request', side_effect=MaxRetryError(None, '/x')), \
                self.assertLogs('main.cloud_storage', 'ERROR'), \
                self.assertRaisesMessage(ImageStorageError, 'Could not reach Cloudinary'):
            HybridMediaStorage().save('hotels/x.png', ContentFile(picture(), name='x.png'))


# ── 3b. Free PythonAnywhere accounts need the outgoing proxy ──────────────

@override_settings(**CLOUD)
class ProxyTests(SimpleTestCase):

    def setUp(self):
        import cloudinary
        import cloudinary.uploader
        env = no_proxy_environment()
        self.addCleanup(env.stop)
        # Put the library back exactly as it was after each test.
        uploader = cloudinary.uploader
        saved = (uploader._http, getattr(uploader, '_iligan_proxy', None), cloudinary.config().api_proxy)

        def restore():
            uploader._http = saved[0]
            if saved[1] is None:
                if hasattr(uploader, '_iligan_proxy'):
                    del uploader._iligan_proxy
            else:
                uploader._iligan_proxy = saved[1]
            cloudinary.config(api_proxy=saved[2])
        self.addCleanup(restore)
        self.uploader = uploader

    def test_without_a_proxy_the_library_connects_directly(self):
        import urllib3
        cloud_storage._uploader()
        self.assertNotIsInstance(self.uploader._http, urllib3.ProxyManager)

    @override_settings(CLOUDINARY_API_PROXY='http://proxy.server:3128')
    def test_the_proxy_setting_makes_the_library_use_the_proxy(self):
        import urllib3
        cloud_storage._uploader()
        self.assertIsInstance(self.uploader._http, urllib3.ProxyManager)
        self.assertEqual(str(self.uploader._http.proxy.host), 'proxy.server')
        self.assertEqual(self.uploader._http.proxy.port, 3128)

    def test_the_standard_https_proxy_variable_is_used_when_the_setting_is_blank(self):
        import urllib3
        with mock.patch.dict(os.environ, {'HTTPS_PROXY': 'http://proxy.server:3128'}):
            cloud_storage._uploader()
        self.assertIsInstance(self.uploader._http, urllib3.ProxyManager)

    def test_the_setting_wins_over_the_environment_variable(self):
        with override_settings(CLOUDINARY_API_PROXY='http://first.example:1111'), \
                mock.patch.dict(os.environ, {'HTTPS_PROXY': 'http://second.example:2222'}):
            cloud_storage._uploader()
        self.assertEqual(str(self.uploader._http.proxy.host), 'first.example')

    def test_switching_the_proxy_off_again_goes_back_to_direct(self):
        import urllib3
        with override_settings(CLOUDINARY_API_PROXY='http://proxy.server:3128'):
            cloud_storage._uploader()
        cloud_storage._uploader()
        self.assertNotIsInstance(self.uploader._http, urllib3.ProxyManager)

    def test_the_pool_is_not_rebuilt_on_every_upload(self):
        with override_settings(CLOUDINARY_API_PROXY='http://proxy.server:3128'):
            cloud_storage._uploader()
            first = self.uploader._http
            cloud_storage._uploader()
            self.assertIs(self.uploader._http, first)

    @override_settings(CLOUDINARY_API_PROXY='http://proxy.server:3128')
    def test_uploads_really_travel_through_the_proxy(self):
        """The real library sends its request to the proxy address, not straight to Cloudinary."""
        import urllib3
        seen = {}

        def fake_urlopen(pool, method, url, **kw):
            seen['host'] = pool.proxy.host
            seen['url'] = url
            raise urllib3.exceptions.MaxRetryError(pool, url)

        with mock.patch('urllib3.poolmanager.ProxyManager.urlopen', fake_urlopen), \
                self.assertLogs('main.cloud_storage', 'ERROR'), \
                self.assertRaisesMessage(ImageStorageError, 'Could not reach Cloudinary'):
            HybridMediaStorage().save('hotels/x.png', ContentFile(picture(), name='x.png'))
        self.assertIn('api.cloudinary.com', seen['url'])
        self.assertEqual(seen['host'], 'proxy.server')


# ── 4. Admin panel views ──────────────────────────────────────────────────

KINDS = ('attraction', 'hotel', 'event', 'room')


@override_settings(**CLOUD)
class PanelImageTests(BaseCase):

    def setUp(self):
        super().setUp()
        blocker = mock.patch('urllib3.connectionpool.HTTPConnectionPool.urlopen',
                             side_effect=AssertionError('Test tried to make a real network call'))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.client = self.login(self.admin)

    # -- helpers
    def data(self, kind, **extra):
        base = {
            'attraction': dict(name='Pool', description='d', location='Iligan', entrance_fee='10',
                               latitude='8.2', longitude='124.2'),
            'hotel': dict(name='Inn', description='d', address='Iligan', stars='3', is_active='on',
                          latitude='8.2', longitude='124.2'),
            'event': dict(name='Fest', description='d', date='2030-01-01', location='Plaza',
                          latitude='8.2', longitude='124.2'),
            'room': dict(hotel=self.hotel.pk, room_number='900', price='500', max_guests='2'),
        }[kind]
        return dict(base, **extra)

    model = {'attraction': Attraction, 'hotel': Hotel, 'event': Event, 'room': Room}

    def existing(self, kind):
        return {'attraction': self.attraction, 'hotel': self.hotel, 'event': self.event, 'room': self.room}[kind]

    def add(self, kind, **files):
        return self.client.post(reverse(f'admin_{kind}_add'), dict(self.data(kind), **files))

    def edit(self, kind, obj, **files):
        return self.client.post(reverse(f'admin_{kind}_edit', args=[obj.pk]), dict(self.data(kind), **files))

    def newest(self, kind):
        return self.model[kind].objects.latest('pk')

    # -- add
    def test_add_with_a_valid_picture_stores_it_on_cloudinary(self):
        for kind in KINDS:
            with self.subTest(kind), FakeCloudinary() as fake:
                response = self.add(kind, image=upload('a.png'))
                self.assertEqual(response.status_code, 302)
                image = self.newest(kind).image
                self.assertRegex(image.name, rf'^cld/iligan_tourism/{kind}s/[A-Za-z0-9_-]+_[0-9a-f]{{8}}$')
                self.assertEqual(len(fake.uploads), 1)
                self.assertTrue(image.url.startswith('https://res.cloudinary.com/demo-cloud/image/upload/'))

    def test_add_without_a_picture_still_works_and_calls_nobody(self):
        for kind in KINDS:
            with self.subTest(kind), FakeCloudinary() as fake:
                self.assertEqual(self.add(kind).status_code, 302)
                self.assertFalse(self.newest(kind).image)
                self.assertEqual(fake.uploads, [])

    def test_add_with_a_bad_file_saves_the_record_without_the_picture_and_explains(self):
        for kind in KINDS:
            with self.subTest(kind), FakeCloudinary() as fake:
                before = self.model[kind].objects.count()
                response = self.add(kind, image=upload('virus.jpg', content=b'not a picture'))
                self.assertEqual(self.model[kind].objects.count(), before + 1)
                self.assertFalse(self.newest(kind).image)
                self.assertEqual(fake.uploads, [])
                self.assertTrue(any('picture was not' in m and 'not a readable picture' in m
                                    for m in self.messages(response)))

    def test_add_when_cloudinary_fails_saves_the_record_and_explains(self):
        from cloudinary.exceptions import Error
        for kind in KINDS:
            with self.subTest(kind), FakeCloudinary(upload_error=Error('Socket error: x')), \
                    self.assertLogs('main.cloud_storage', 'ERROR'):
                before = self.model[kind].objects.count()
                response = self.add(kind, image=upload('a.png'))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.model[kind].objects.count(), before + 1)
                self.assertFalse(self.newest(kind).image)
                self.assertTrue(any('Could not reach Cloudinary' in m for m in self.messages(response)))

    # -- edit
    def test_edit_without_a_new_file_keeps_the_picture_and_changes_the_other_fields(self):
        for kind in KINDS:
            obj = self.existing(kind)
            self.model[kind].objects.filter(pk=obj.pk).update(image='cld/iligan_tourism/x/keep_1a2b3c4d')
            with self.subTest(kind), FakeCloudinary() as fake:
                self.edit(kind, obj)
                obj.refresh_from_db()
                self.assertEqual(obj.image.name, 'cld/iligan_tourism/x/keep_1a2b3c4d')
                self.assertEqual((fake.uploads, fake.destroys), ([], []))

    def test_replacing_a_cloudinary_picture_removes_the_old_one_after_the_save(self):
        for kind in KINDS:
            obj = self.existing(kind)
            self.model[kind].objects.filter(pk=obj.pk).update(image='cld/iligan_tourism/x/old_1a2b3c4d')
            with self.subTest(kind), FakeCloudinary() as fake, self.captureOnCommitCallbacks(execute=True):
                self.edit(kind, obj, image=upload('new.png'))
                obj.refresh_from_db()
                self.assertNotEqual(obj.image.name, 'cld/iligan_tourism/x/old_1a2b3c4d')
                self.assertTrue(obj.image.name.startswith(CLOUD_PREFIX))
            self.assertEqual(fake.destroys, ['iligan_tourism/x/old_1a2b3c4d'])

    def test_the_old_picture_is_not_removed_until_the_change_is_committed(self):
        obj = self.attraction
        Attraction.objects.filter(pk=obj.pk).update(image='cld/iligan_tourism/x/old_1a2b3c4d')
        with FakeCloudinary() as fake, self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.edit('attraction', obj, image=upload('new.png'))
        self.assertEqual(fake.destroys, [])
        self.assertTrue(callbacks)

    def test_failed_upload_during_edit_keeps_the_old_picture_and_saves_the_text(self):
        from cloudinary.exceptions import Error
        obj = self.attraction
        Attraction.objects.filter(pk=obj.pk).update(image='cld/iligan_tourism/x/old_1a2b3c4d')
        with FakeCloudinary(upload_error=Error('Unexpected error - x')) as fake, \
                self.assertLogs('main.cloud_storage', 'ERROR'), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('admin_attraction_edit', args=[obj.pk]),
                                       dict(self.data('attraction'), name='Renamed', image=upload('new.png')))
        obj.refresh_from_db()
        self.assertEqual((obj.name, obj.image.name), ('Renamed', 'cld/iligan_tourism/x/old_1a2b3c4d'))
        self.assertEqual(fake.destroys, [])                 # the old picture must survive
        self.assertTrue(any('picture was not' in m for m in self.messages(response)))

    def test_bad_file_during_edit_keeps_the_old_picture(self):
        obj = self.hotel
        Hotel.objects.filter(pk=obj.pk).update(image='cld/iligan_tourism/x/old_1a2b3c4d')
        with FakeCloudinary() as fake, self.captureOnCommitCallbacks(execute=True):
            self.edit('hotel', obj, image=upload('x.exe', content=b'MZ'))
        obj.refresh_from_db()
        self.assertEqual(obj.image.name, 'cld/iligan_tourism/x/old_1a2b3c4d')
        self.assertEqual((fake.uploads, fake.destroys), ([], []))

    def test_replacing_an_old_local_picture_never_deletes_the_local_file(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            old = Path(folder) / 'attractions' / 'timoga.png'
            old.parent.mkdir()
            old.write_bytes(picture())
            Attraction.objects.filter(pk=self.attraction.pk).update(image='attractions/timoga.png')
            with FakeCloudinary() as fake, self.captureOnCommitCallbacks(execute=True):
                self.edit('attraction', self.attraction, image=upload('new.png'))
            self.assertTrue(old.exists())
            self.assertEqual(fake.destroys, [])
            self.attraction.refresh_from_db()
            self.assertTrue(self.attraction.image.name.startswith(CLOUD_PREFIX))

    # -- delete
    def test_deleting_a_record_removes_its_cloudinary_picture(self):
        for kind in ('attraction', 'event', 'room', 'hotel'):       # hotel last: it takes a room with it
            obj = self.model[kind].objects.get(pk=self.existing(kind).pk)
            self.model[kind].objects.filter(pk=obj.pk).update(image='cld/iligan_tourism/x/gone_1a2b3c4d')
            expected = ['iligan_tourism/x/gone_1a2b3c4d']
            if kind == 'hotel':
                Room.objects.create(hotel=obj, room_number='777', price=1, image='cld/iligan_tourism/x/room_1a2b3c4d')
                expected.append('iligan_tourism/x/room_1a2b3c4d')       # the hotel's room goes with it
            with self.subTest(kind), FakeCloudinary() as fake, self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse(f'admin_{kind}_delete', args=[obj.pk]))
            self.assertFalse(self.model[kind].objects.filter(pk=obj.pk).exists())
            self.assertCountEqual(fake.destroys, expected)

    def test_deleting_a_record_with_a_local_picture_does_not_call_cloudinary(self):
        Event.objects.filter(pk=self.event.pk).update(image='events/concert1.jpg')
        with FakeCloudinary() as fake, self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('admin_event_delete', args=[self.event.pk]))
        self.assertEqual(fake.destroys, [])

    def test_a_failing_cleanup_does_not_break_the_delete(self):
        Attraction.objects.filter(pk=self.attraction.pk).update(image='cld/x/y_1')
        with FakeCloudinary(destroy_error=OSError('down')), self.assertLogs('main.cloud_storage', 'WARNING'), \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('admin_attraction_delete', args=[self.attraction.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Attraction.objects.filter(pk=self.attraction.pk).exists())

    # -- access control
    def test_only_admins_can_upload(self):
        for who in (None, self.customer, self.hotel_staff):
            client = self.login(who) if who else self.client_class()
            with self.subTest(who), FakeCloudinary() as fake:
                before = Attraction.objects.count()
                client.post(reverse('admin_attraction_add'), dict(self.data('attraction'), image=upload()))
                self.assertEqual(Attraction.objects.count(), before)
                self.assertEqual(fake.uploads, [])

    # -- the forms
    def test_forms_show_the_rules_and_where_pictures_are_stored(self):
        for kind in KINDS:
            with self.subTest(kind):
                page = self.client.get(reverse(f'admin_{kind}_add'))
                self.assertContains(page, 'up to 8 MB')
                self.assertContains(page, 'Stored on: Cloudinary.')
                self.assertContains(page, 'accept="image/jpeg,image/png,image/webp,image/gif"')
        with override_settings(**NO_CLOUD):
            self.assertContains(self.client.get(reverse('admin_hotel_add')), 'Stored on: this server.')
        with override_settings(CLOUDINARY_API_SECRET=''):
            self.assertContains(self.client.get(reverse('admin_hotel_add')), 'Cloudinary settings are incomplete')

    # -- pictures show up on the public pages
    def test_public_pages_use_the_right_address_for_each_kind_of_picture(self):
        Attraction.objects.filter(pk=self.attraction.pk).update(image='cld/iligan_tourism/attractions/a_1a2b3c4d', is_featured=True)
        Hotel.objects.filter(pk=self.hotel.pk).update(image='hotels/Gohotel.png')
        Event.objects.filter(pk=self.event.pk).update(image='cld/iligan_tourism/events/e_1a2b3c4d')
        Room.objects.filter(pk=self.room.pk).update(image='cld/iligan_tourism/rooms/r_1a2b3c4d')
        cloud = 'https://res.cloudinary.com/demo-cloud/image/upload/c_limit,w_1600,f_auto,q_auto/iligan_tourism/'
        pages = [
            ('home', [], [cloud + 'attractions/a_1a2b3c4d', '/media/hotels/Gohotel.png', cloud + 'events/e_1a2b3c4d']),
            ('attractions', [], [cloud + 'attractions/a_1a2b3c4d']),
            ('attraction_detail', [self.attraction.pk], [cloud + 'attractions/a_1a2b3c4d']),
            ('hotels', [], ['/media/hotels/Gohotel.png']),
            ('hotel_detail', [self.hotel.pk], ['/media/hotels/Gohotel.png']),
            ('room_detail', [self.room.pk], [cloud + 'rooms/r_1a2b3c4d']),
            ('events', [], [cloud + 'events/e_1a2b3c4d']),
            ('event_detail', [self.event.pk], [cloud + 'events/e_1a2b3c4d']),
        ]
        for name, args, expected in pages:
            with self.subTest(name):
                response = self.client.get(reverse(name, args=args))
                self.assertEqual(response.status_code, 200)
                for address in expected:
                    self.assertContains(response, address)


# ── 5. Django's own /admin/ site ──────────────────────────────────────────

@override_settings(**CLOUD)
class DjangoAdminImageTests(BaseCase):

    def setUp(self):
        super().setUp()
        superuser = User.objects.create_superuser('boss', 'b@example.com', PASSWORD)
        self.client = self.login(superuser)

    def event_data(self, **extra):
        return dict(name='Fest', description='d', date='2030-01-01', location='Plaza',
                    ticket_price='0', ticket_capacity='0', **extra)

    def test_a_valid_picture_goes_to_cloudinary(self):
        with FakeCloudinary() as fake:
            response = self.client.post(reverse('admin:main_event_add'), self.event_data(image=upload()))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Event.objects.get(name='Fest').image.name.startswith(CLOUD_PREFIX))
        self.assertEqual(len(fake.uploads), 1)

    def test_a_bad_file_is_rejected_on_the_form_and_nothing_is_saved(self):
        with FakeCloudinary() as fake:
            response = self.client.post(reverse('admin:main_event_add'),
                                        self.event_data(image=upload('x.jpg', content=b'nope')))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Event.objects.filter(name='Fest').exists())
        self.assertEqual(fake.uploads, [])

    def test_an_oversized_picture_is_rejected_with_the_limit(self):
        with override_settings(IMAGE_MAX_UPLOAD_MB=0), FakeCloudinary():
            response = self.client.post(reverse('admin:main_event_add'), self.event_data(image=upload()))
        self.assertContains(response, 'The limit is 0 MB')

    def test_a_cloudinary_failure_saves_the_rest_and_shows_a_message(self):
        from cloudinary.exceptions import Error
        with FakeCloudinary(upload_error=Error('Socket error: x')), self.assertLogs('main.cloud_storage', 'ERROR'):
            response = self.client.post(reverse('admin:main_event_add'), self.event_data(image=upload()), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Event.objects.get(name='Fest').image)
        self.assertContains(response, 'the picture was not stored')

    def test_failed_edit_keeps_the_previous_picture(self):
        from cloudinary.exceptions import Error
        Event.objects.filter(pk=self.event.pk).update(image='cld/x/old_1')
        with FakeCloudinary(upload_error=Error('Socket error: x')), self.assertLogs('main.cloud_storage', 'ERROR'):
            self.client.post(reverse('admin:main_event_change', args=[self.event.pk]),
                             self.event_data(image=upload()))
        self.event.refresh_from_db()
        self.assertEqual(self.event.image.name, 'cld/x/old_1')


# ── 6. cloudinary_tools.py (live check and migration) ─────────────────────

def load_tools():
    spec = importlib.util.spec_from_file_location('cloudinary_tools', settings.BASE_DIR / 'cloudinary_tools.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeWebReply:
    def __init__(self, kind='image/png', body=b'12345'):
        self.headers, self._body = {'Content-Type': kind}, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@override_settings(**CLOUD)
class ToolsTests(BaseCase):

    def setUp(self):
        super().setUp()
        self.tools = load_tools()
        self.tools._setup = lambda: (settings, cloud_storage)       # Django is already set up in tests

    def run_tool(self, function, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = function(*args)
        return code, out.getvalue()

    def test_check_succeeds_and_cleans_up(self):
        with FakeCloudinary() as fake, mock.patch('urllib.request.urlopen', return_value=FakeWebReply()):
            code, text = self.run_tool(self.tools.check)
        self.assertEqual(code, 0)
        self.assertIn('RESULT: Cloudinary works.', text)
        self.assertEqual(len(fake.uploads), 1)
        self.assertEqual(len(fake.destroys), 1)
        self.assertNotIn(API_SECRET, text)
        self.assertNotIn(API_KEY, text)

    def test_check_reports_a_picture_that_cannot_be_displayed(self):
        with FakeCloudinary() as fake, mock.patch('urllib.request.urlopen', return_value=FakeWebReply('text/html')):
            code, text = self.run_tool(self.tools.check)
        self.assertEqual(code, 1)
        self.assertIn('FAIL', text)
        self.assertEqual(len(fake.destroys), 1)                      # still cleans up

    def test_check_reports_an_upload_failure_without_leaking_keys(self):
        from cloudinary.exceptions import Error
        with FakeCloudinary(upload_error=Error(f'Invalid api_key {API_KEY}')), self.assertLogs('main.cloud_storage', 'ERROR'):
            code, text = self.run_tool(self.tools.check)
        self.assertEqual(code, 1)
        self.assertIn('did not accept the account details', text)
        self.assertNotIn(API_KEY, text)

    @override_settings(CLOUDINARY_API_SECRET='')
    def test_check_explains_missing_settings(self):
        code, text = self.run_tool(self.tools.check)
        self.assertEqual(code, 1)
        self.assertIn('partial', text)

    def make_local(self, folder, relative):
        path = Path(folder) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(picture())
        return path

    def test_migrate_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder), FakeCloudinary() as fake:
            self.make_local(folder, 'attractions/a.png')
            Attraction.objects.filter(pk=self.attraction.pk).update(image='attractions/a.png')
            code, text = self.run_tool(self.tools.migrate, False)
        self.assertEqual(code, 0)
        self.assertIn('WOULD MOVE', text)
        self.assertEqual(fake.uploads, [])
        self.attraction.refresh_from_db()
        self.assertEqual(self.attraction.image.name, 'attractions/a.png')

    def test_migrate_apply_moves_local_pictures_and_keeps_the_files(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder), FakeCloudinary() as fake:
            files = {
                (Attraction, self.attraction.pk): 'attractions/a.png', (Hotel, self.hotel.pk): 'hotels/h.png',
                (Event, self.event.pk): 'events/e.jpg', (Room, self.room.pk): 'rooms/r.png',
            }
            for (model, pk), name in files.items():
                self.make_local(folder, name)
                model.objects.filter(pk=pk).update(image=name)
            code, text = self.run_tool(self.tools.migrate, True)
            self.assertEqual(code, 0)
            self.assertIn('4 moved', text)
            self.assertEqual(len(fake.uploads), 4)
            for (model, pk), name in files.items():
                self.assertTrue(model.objects.get(pk=pk).image.name.startswith(CLOUD_PREFIX))
                self.assertTrue((Path(folder) / name).is_file())      # originals are kept
            # Running it again does nothing (safe to repeat).
            code, text = self.run_tool(self.tools.migrate, True)
            self.assertIn('0 moved, 4 already on Cloudinary', text)
            self.assertEqual(len(fake.uploads), 4)

    def test_migrate_reports_missing_files_and_failures_and_leaves_those_rows_alone(self):
        from cloudinary.exceptions import Error
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            Attraction.objects.filter(pk=self.attraction.pk).update(image='attractions/ghost.png')
            self.make_local(folder, 'hotels/h.png')
            Hotel.objects.filter(pk=self.hotel.pk).update(image='hotels/h.png')
            with FakeCloudinary(upload_error=Error('Socket error: x')), self.assertLogs('main.cloud_storage', 'ERROR'):
                code, text = self.run_tool(self.tools.migrate, True)
        self.assertEqual(code, 1)
        self.assertIn('MISSING FILE', text)
        self.assertIn('FAILED', text)
        self.assertEqual(Attraction.objects.get(pk=self.attraction.pk).image.name, 'attractions/ghost.png')
        self.assertEqual(Hotel.objects.get(pk=self.hotel.pk).image.name, 'hotels/h.png')

    @override_settings(**NO_CLOUD)
    def test_migrate_refuses_to_run_without_cloudinary_settings(self):
        code, text = self.run_tool(self.tools.migrate, True)
        self.assertEqual(code, 1)
        self.assertIn('not configured', text)

    def test_command_line_dispatch(self):
        with mock.patch.object(self.tools, 'check', return_value=0) as check, \
                mock.patch.object(self.tools, 'migrate', return_value=0) as migrate:
            self.tools.main(['x', 'check'])
            self.tools.main(['x', 'migrate'])
            self.tools.main(['x', 'migrate', '--apply'])
        check.assert_called_once_with()
        self.assertEqual([c.kwargs for c in migrate.call_args_list], [{'apply': False}, {'apply': True}])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.tools.main(['x']), 2)


# ── 7. Project wiring ─────────────────────────────────────────────────────

class WiringTests(SimpleTestCase):

    def test_requirements_list_the_cloudinary_package(self):
        text = (settings.BASE_DIR / 'requirements.txt').read_text()
        self.assertRegex(text, r'(?m)^cloudinary==\d+\.\d+\.\d+\s*$')

    def test_env_example_documents_the_new_settings_without_values(self):
        text = (settings.BASE_DIR / '.env.example').read_text()
        for key in ('CLOUDINARY_CLOUD_NAME=', 'CLOUDINARY_API_KEY=', 'CLOUDINARY_API_SECRET='):
            self.assertRegex(text, rf'(?m)^{key}\s*$')

    def test_no_secret_is_hardcoded_in_the_new_code(self):
        for relative in ('main/cloud_storage.py', 'main/signals.py', 'cloudinary_tools.py',
                         'iligan_tourism_system/settings.py'):
            source = (settings.BASE_DIR / relative).read_text()
            self.assertNotRegex(source, r'cloudinary://')
            self.assertNotRegex(source, r'api_secret\s*=\s*[\'"][^\'"]+[\'"]')
