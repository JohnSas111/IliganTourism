"""
Developer tool for the Cloudinary image integration (Batch 5).

Run it from the project folder (the one with manage.py):

    py cloudinary_tools.py check              real test: upload a tiny picture, open it, delete it
    py cloudinary_tools.py migrate            SHOW which existing local pictures would move
    py cloudinary_tools.py migrate --apply    actually move them to Cloudinary

It reads the same .env values as the website. It NEVER prints your API key or secret.
It contains no secrets, so it is safe to commit.

"migrate" copies each local picture to Cloudinary and points the database at the copy.
The original files in media/ are NOT deleted, so nothing is lost. Back up db.sqlite3 first.
"""
import io
import os
import sys
import urllib.error
import urllib.request

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'iligan_tourism_system.settings')


def _setup():
    import django
    django.setup()
    from django.conf import settings
    from main import cloud_storage
    return settings, cloud_storage


def _tiny_png():
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (64, 64), (30, 130, 90)).save(buffer, 'PNG')
    return buffer.getvalue()


def check():
    settings, cs = _setup()
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    status = cs.cloudinary_status()
    print(f'Cloudinary settings: {status}')
    if status != 'ready':
        print('FAIL: CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET must all be')
        print('      filled in your .env file (copy them from console.cloudinary.com > Settings > API Keys).')
        return 1
    print(f'Cloud name: {settings.CLOUDINARY_CLOUD_NAME}')
    proxy = cs._proxy_address()
    if proxy:
        print(f'Proxy: {proxy.split("@")[-1]}')
    else:
        print('Proxy: none (on a FREE PythonAnywhere account you need CLOUDINARY_API_PROXY=http://proxy.server:3128)')

    print('1/3 Uploading a 64x64 test picture ...')
    try:
        name = default_storage.save('checks/cloudinary-check.png', ContentFile(_tiny_png()))
    except cs.ImageStorageError as error:
        print(f'FAIL: {error}')
        print('      (the server log line above has the technical reason; your key is hidden there)')
        return 1
    print(f'      OK, stored as: {name}')

    print('2/3 Opening the picture from Cloudinary like a visitor would ...')
    url = default_storage.url(name)
    print(f'      {url}')
    code = 1
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'IliganTourismCheck/1.0'})
        with urllib.request.urlopen(request, timeout=30) as response:
            kind = response.headers.get('Content-Type', '')
            size = len(response.read())
        if kind.startswith('image/'):
            print(f'      OK, {kind}, {size} bytes')
            code = 0
        else:
            print(f'FAIL: got "{kind}" instead of a picture')
    except urllib.error.HTTPError as error:
        print(f'FAIL: res.cloudinary.com answered HTTP {error.code}')
    except (urllib.error.URLError, OSError) as error:
        print(f'FAIL: could not open the picture ({error})')

    print('3/3 Deleting the test picture ...')
    default_storage.delete(name)
    print('      Done (if this was not deleted, remove "iligan_tourism/checks" in your Cloudinary Media Library).')
    print('RESULT: Cloudinary works.' if code == 0 else 'RESULT: upload worked but the picture could not be displayed.')
    return code


def migrate(apply):
    settings, cs = _setup()
    from django.core.files import File
    from django.core.files.storage import default_storage
    from main.models import Attraction, Event, Hotel, Room

    if cs.cloudinary_status() != 'ready':
        print('Cloudinary is not configured in .env, nothing to do. Run "py cloudinary_tools.py check" first.')
        return 1

    media_root = str(settings.MEDIA_ROOT)
    moved = missing = failed = already = 0
    for model in (Attraction, Hotel, Event, Room):
        for obj in model.objects.exclude(image='').exclude(image__isnull=True):
            name = obj.image.name
            label = f'{model.__name__} #{obj.pk} ({name})'
            if cs.is_cloud_name(name):
                already += 1
                continue
            path = os.path.join(media_root, name)
            if not os.path.isfile(path):
                print(f'MISSING FILE  {label}: not found in media/, left unchanged')
                missing += 1
                continue
            if not apply:
                print(f'WOULD MOVE    {label}')
                moved += 1
                continue
            try:
                with open(path, 'rb') as handle:
                    new_name = default_storage.save(name, File(handle, name=os.path.basename(name)))
            except cs.ImageStorageError as error:
                print(f'FAILED        {label}: {error}')
                failed += 1
                continue
            model.objects.filter(pk=obj.pk).update(image=new_name)
            print(f'MOVED         {label} -> {new_name}')
            moved += 1

    verb = 'moved' if apply else 'would be moved'
    print(f'\nSummary: {moved} {verb}, {already} already on Cloudinary, {missing} missing file(s), {failed} failed.')
    if not apply and moved:
        print('Nothing was changed. To do it for real, run: py cloudinary_tools.py migrate --apply')
    return 1 if failed else 0


def main(argv):
    if len(argv) >= 2 and argv[1] == 'check':
        return check()
    if len(argv) >= 2 and argv[1] == 'migrate':
        return migrate(apply='--apply' in argv[2:])
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
