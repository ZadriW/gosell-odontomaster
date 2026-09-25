# vim: syntax=python
import glob
import os
import sys

_wsgi_dir = os.path.dirname(os.path.abspath(__file__))


def _resolve_project_root():
    explicit = os.environ.get("APP_ROOT", "").strip()
    if explicit:
        return explicit
    if os.path.isfile(os.path.join(_wsgi_dir, "app.py")):
        return _wsgi_dir
    nested = os.path.join(_wsgi_dir, "soumaismaster")
    if os.path.isfile(os.path.join(nested, "app.py")):
        return nested
    return _wsgi_dir


def _prepend_venv_site_packages(*roots):
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirname in ("virtual_env", "venv", ".venv"):
            base = os.path.join(root, dirname)
            if not os.path.isdir(base):
                continue
            pattern = os.path.join(base, "lib", "python*", "site-packages")
            for sp in glob.glob(pattern):
                if os.path.isdir(sp) and sp not in seen:
                    sys.path.insert(0, sp)
                    seen.add(sp)
            break


_project_root = _resolve_project_root()
_prepend_venv_site_packages(_project_root, _wsgi_dir)

os.chdir(_project_root)

for _p in (_project_root, _wsgi_dir):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from app import app as application
